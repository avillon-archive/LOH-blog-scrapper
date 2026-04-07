# -*- coding: utf-8 -*-
"""데이터 클래스 및 타입 정의."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from bs4 import BeautifulSoup

from utils import (
    append_line,
    filter_file_lines,
    load_failed_post_urls,
    remove_lines_by_prefix,
)

# ---------------------------------------------------------------------------
# 타입 별칭
# ---------------------------------------------------------------------------

PostSoupCache = dict[str, tuple[BeautifulSoup, str] | None] | None


# ---------------------------------------------------------------------------
# NamedTuple / dataclass
# ---------------------------------------------------------------------------


class KakaoPFPost(NamedTuple):
    id: int
    title: str
    published_at: int  # Unix ms
    media_urls: list[str]


@dataclass
class PostProcessResult:
    ok: int
    fail: int
    post_fetch_ok: bool
    ok_saved: int = 0
    ok_original: int = 0
    ok_multilang: int = 0
    ok_kakao: int = 0
    ok_dedup: int = 0
    succeeded_urls: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ImageFailedLog
# ---------------------------------------------------------------------------


class ImageFailedLog:
    """download_images 전용 실패 이력 관리.

    utils.FailedLog 는 2-tuple(post_url, reason) 기반이므로,
    img_url 을 포함하는 3-tuple 구조를 위해 독립 클래스로 구현한다.
    """

    def __init__(self, filepath: Path, lock: threading.Lock) -> None:
        self._filepath = filepath
        self._lock = lock
        self._cache: set[tuple[str, str, str]] | None = None

    def _load(self) -> set[tuple[str, str, str]]:
        entries: set[tuple[str, str, str]] = set()
        if not self._filepath.exists():
            return entries
        for line in self._filepath.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                post_url = parts[0].strip()
                img_url = parts[1].strip()
                reason = parts[2].strip()
                if post_url and reason:
                    entries.add((post_url, img_url, reason))
        return entries

    def record(self, post_url: str, img_url: str, reason: str) -> None:
        key = (post_url, img_url, reason)
        with self._lock:
            if self._cache is None:
                self._cache = self._load()
            if key in self._cache:
                return
            self._cache.add(key)
        append_line(self._filepath, f"{post_url}\t{img_url}\t{reason}")

    def remove(self, post_url: str, reason: str | None = None,
               img_url: str | None = None) -> None:
        if not self._filepath.exists():
            return
        prefix = post_url + "\t"
        if img_url is not None:
            def _keep(line: str) -> bool:
                if not line.startswith(prefix):
                    return True
                parts = line.split("\t")
                return (parts[1].strip() if len(parts) >= 2 else "") != img_url
            filter_file_lines(self._filepath, _keep)
            with self._lock:
                if self._cache is None:
                    self._cache = self._load()
                self._cache = {e for e in self._cache if not (e[0] == post_url and e[1] == img_url)}
        elif reason is None:
            remove_lines_by_prefix(self._filepath, prefix)
            with self._lock:
                if self._cache is None:
                    self._cache = self._load()
                self._cache = {e for e in self._cache if e[0] != post_url}
        else:
            def _keep(line: str) -> bool:
                if not line.startswith(prefix):
                    return True
                parts = line.split("\t")
                return (parts[2].strip() if len(parts) >= 3 else "") != reason
            filter_file_lines(self._filepath, _keep)
            with self._lock:
                if self._cache is None:
                    self._cache = self._load()
                self._cache = {e for e in self._cache if not (e[0] == post_url and e[2] == reason)}

    def remove_batch(self, post_url: str, img_urls: set[str]) -> None:
        """post_url에 속하는 여러 img_url 엔트리를 한 번의 파일 I/O로 제거한다."""
        if not img_urls or not self._filepath.exists():
            return
        prefix = post_url + "\t"

        def _keep(line: str) -> bool:
            if not line.startswith(prefix):
                return True
            parts = line.split("\t")
            return (parts[1].strip() if len(parts) >= 2 else "") not in img_urls
        filter_file_lines(self._filepath, _keep)
        with self._lock:
            if self._cache is None:
                self._cache = self._load()
            self._cache = {e for e in self._cache
                           if not (e[0] == post_url and e[1] in img_urls)}

    def load_post_urls(self) -> set[str]:
        return load_failed_post_urls(self._filepath)
