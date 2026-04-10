# -*- coding: utf-8 -*-
"""download_media 파일 I/O: media_map, seen_urls, done_posts, failed."""

from pathlib import Path

from log_io import _is_header, _split_row

from .constants import (
    DONE_POSTS_MEDIA_HEADER,
    FAILED_MEDIA_HEADER,
    MEDIA_MAP_HEADER,
)
from .state import _failed_media_log


def load_media_map_entries(
    filepath: Path,
) -> list[tuple[str, str, str, str, str]]:
    """media_map.csv → [(post_url, media_url, rel_path, anchor_type, anchor_text), ...]."""
    rows: list[tuple[str, str, str, str, str]] = []
    if not filepath.exists():
        return rows
    for line in filepath.read_text(encoding="utf-8").splitlines():
        row = line.strip().lstrip("\ufeff")
        if not row or _is_header(row, MEDIA_MAP_HEADER):
            continue
        parts = _split_row(row)
        if len(parts) < 3:
            continue
        post_url = parts[0]
        media_url = parts[1]
        rel_path = parts[2]
        anchor_type = parts[3] if len(parts) >= 4 else "inline"
        anchor_text = parts[4] if len(parts) >= 5 else ""
        if post_url and media_url and rel_path:
            rows.append((post_url, media_url, rel_path, anchor_type, anchor_text))
    return rows


def load_media_url_to_path(filepath: Path) -> dict[str, str]:
    """media_map.csv → {clean_media_url: rel_path} (URL 단위 중복 제거, 첫 출현 우선)."""
    result: dict[str, str] = {}
    for _post_url, media_url, rel_path, _atype, _atext in load_media_map_entries(filepath):
        if media_url not in result:
            result[media_url] = rel_path
    return result


def load_seen_media(filepath: Path) -> set[str]:
    seen: set[str] = set()
    if not filepath.exists():
        return seen
    for line in filepath.read_text(encoding="utf-8").splitlines():
        row = line.strip()
        if row:
            seen.add(row)
    return seen


def load_done_posts_media(filepath: Path) -> dict[str, int]:
    if not filepath.exists():
        return {}
    result: dict[str, int] = {}
    for line in filepath.read_text(encoding="utf-8").splitlines():
        row = line.strip().lstrip("\ufeff")
        if not row or _is_header(row, DONE_POSTS_MEDIA_HEADER):
            continue
        parts = _split_row(row)
        if len(parts) >= 2 and parts[0]:
            try:
                result[parts[0]] = int(parts[1])
            except ValueError:
                result[parts[0]] = 0
    return result


def record_failed_media(post_url: str, media_url: str, reason: str) -> None:
    _failed_media_log.record(post_url, media_url, reason)


def remove_from_failed_media(
    post_url: str,
    reason: str | None = None,
    media_url: str | None = None,
) -> None:
    _failed_media_log.remove(post_url, reason, img_url=media_url)
