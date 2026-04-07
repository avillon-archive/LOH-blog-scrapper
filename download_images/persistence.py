# -*- coding: utf-8 -*-
"""파일 저장, seen/done/failed 헬퍼, backfill."""

import re
from pathlib import Path

from utils import ROOT_DIR, append_line, load_image_map

from .constants import (
    DONE_FILE,
    DONE_POSTS_FILE,
    DOWNLOADABLE_EXTS,
    IMAGE_MAP_FILE,
    IMAGES_DIR,
)
from .state import _failed_log
from .url_utils import _basename, _clean_img_url, _safe_filename


def save_image(content: bytes, filename: str, folder: Path) -> str:
    """bytes를 folder/filename에 저장하고 충돌을 해소한다.

    동일 내용의 파일이 이미 존재하면 해당 파일명을 반환한다.
    충돌 검사는 크기 비교(cheap) → 바이트 비교(expensive) 순서.
    """
    folder.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename(filename)
    stem = Path(filename).stem
    suffix = Path(filename).suffix or ".bin"
    content_len = len(content)

    target = folder / filename
    idx = 2
    while target.exists():
        try:
            if target.stat().st_size == content_len and target.read_bytes() == content:
                return target.name
        except OSError:
            pass
        target = folder / f"{stem}_{idx}{suffix}"
        idx += 1

    target.write_bytes(content)
    return target.name


def record_image_map(
    clean_url_key: str,
    relative_path: str,
    image_map: dict[str, str],
    filepath: Path,
):
    """image_map 딕셔너리와 파일에 항목을 추가한다 (중복 방지).

    backfill_image_map 전용. download_one_image 는 _state_lock + _map_buf 를 직접 사용.
    """
    if not clean_url_key or not relative_path:
        return
    existing = image_map.get(clean_url_key)
    if existing == relative_path:
        return
    image_map[clean_url_key] = relative_path
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    append_line(filepath, f"{clean_url_key}\t{relative_path}")


# ---------------------------------------------------------------------------
# done / failed 파일 헬퍼
# ---------------------------------------------------------------------------


def _load_done_post_urls(filepath: Path) -> dict[str, int]:
    """이미지 수집이 완료된 포스트 URL → 이미지 수 딕셔너리를 반환한다."""
    if not filepath.exists():
        return {}
    result: dict[str, int] = {}
    for line in filepath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        url = parts[0].strip()
        count = int(parts[1]) if len(parts) >= 2 and parts[1].strip().isdigit() else 0
        result[url] = count
    return result


def _parse_done_line_to_main_url(row: str) -> str | None:
    if row.startswith("thumb:"):
        return None
    if row.startswith("main:"):
        return row.split(":", 1)[1].strip() or None
    return row.strip() or None


def load_seen(filepath: Path) -> set[str]:
    seen: set[str] = set()
    if filepath.exists():
        for line in filepath.read_text(encoding="utf-8").splitlines():
            row = line.strip()
            if not row:
                continue
            if row.startswith("main:") or row.startswith("thumb:"):
                seen.add(row)
            else:
                seen.add(f"main:{row}")
    return seen


def record_failed(post_url: str, img_url: str, reason: str) -> None:
    _failed_log.record(post_url, img_url, reason)


def remove_from_failed(post_url: str, reason: str | None = None,
                       img_url: str | None = None) -> None:
    _failed_log.remove(post_url, reason, img_url=img_url)


def remove_from_failed_batch(post_url: str, img_urls: set[str]) -> None:
    _failed_log.remove_batch(post_url, img_urls)


# ---------------------------------------------------------------------------
# backfill (--backfill-map 옵션)
# ---------------------------------------------------------------------------


def backfill_image_map() -> None:
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    image_map = load_image_map(IMAGE_MAP_FILE)

    files = [
        f
        for f in IMAGES_DIR.rglob("*")
        if f.is_file()
        and f.suffix.lower() in DOWNLOADABLE_EXTS
        and "thumbnails" not in f.relative_to(IMAGES_DIR).parts
    ]

    by_name: dict[str, list[Path]] = {}
    for f in files:
        by_name.setdefault(f.name, []).append(f)

    added = 0
    if DONE_FILE.exists():
        for line in DONE_FILE.read_text(encoding="utf-8").splitlines():
            url = _parse_done_line_to_main_url(line.strip())
            if not url:
                continue
            key = _clean_img_url(url)
            if key in image_map:
                continue
            base = _basename(url)
            if not base:
                continue
            candidates = by_name.get(base, [])
            if not candidates:
                stem = Path(base).stem
                suffix = Path(base).suffix
                pattern = re.compile(rf"^{re.escape(stem)}(?:_\d+)?{re.escape(suffix)}$")
                for name, name_paths in by_name.items():
                    if pattern.match(name):
                        candidates.extend(name_paths)
            if not candidates:
                continue
            chosen = sorted(candidates, key=lambda x: str(x))[0]
            rel = chosen.relative_to(ROOT_DIR).as_posix()
            record_image_map(key, rel, image_map, IMAGE_MAP_FILE)
            added += 1

    print(f"[MAP] backfill added={added} total={len(image_map)}")
