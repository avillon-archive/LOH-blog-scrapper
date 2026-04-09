# -*- coding: utf-8 -*-
"""파일 저장, seen/done/failed 헬퍼."""

from pathlib import Path

from log_io import _is_header, _split_row, csv_line
from utils import ROOT_DIR, append_line

from .constants import (
    DONE_FILE,
    DONE_POSTS_FILE,
)
from .state import _failed_log
from .url_utils import _safe_filename


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



# ---------------------------------------------------------------------------
# done / failed 파일 헬퍼
# ---------------------------------------------------------------------------


def _load_done_post_urls(filepath: Path) -> dict[str, int]:
    """이미지 수집이 완료된 포스트 URL → 이미지 수 딕셔너리를 반환한다."""
    if not filepath.exists():
        return {}
    result: dict[str, int] = {}
    for line in filepath.read_text(encoding="utf-8").splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or _is_header(line, "post_url,image_count"):
            continue
        parts = _split_row(line)
        url = parts[0]
        count = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        result[url] = count
    return result



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


def load_failed_image_entries(filepath: Path) -> dict[str, set[str] | None]:
    """failed_images.txt에서 {post_url: set[clean_img_url] | None} 로드.

    img_url이 빈 엔트리(fetch_post_failed)는 None → 해당 포스트 전체 이미지 재시도.
    """
    from .url_utils import _clean_img_url

    result: dict[str, set[str] | None] = {}
    if not filepath.exists():
        return result
    for line in filepath.read_text(encoding="utf-8").splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or _is_header(line, "post_url,img_url,reason"):
            continue
        parts = _split_row(line)
        if len(parts) < 3:
            continue
        post_url_entry = parts[0]
        img_url_entry = parts[1]
        if not post_url_entry:
            continue
        if not img_url_entry:
            result[post_url_entry] = None
        elif result.get(post_url_entry) is not None or post_url_entry not in result:
            if post_url_entry not in result:
                result[post_url_entry] = set()
            cur = result[post_url_entry]
            if cur is not None:
                cur.add(_clean_img_url(img_url_entry))
    return result


def record_failed(post_url: str, img_url: str, reason: str) -> None:
    _failed_log.record(post_url, img_url, reason)


def remove_from_failed(post_url: str, reason: str | None = None,
                       img_url: str | None = None) -> None:
    _failed_log.remove(post_url, reason, img_url=img_url)


def remove_from_failed_batch(post_url: str, img_urls: set[str]) -> None:
    _failed_log.remove_batch(post_url, img_urls)
