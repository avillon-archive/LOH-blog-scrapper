# -*- coding: utf-8 -*-
"""SHA-256 유틸 및 해시 캐시 로드."""

import hashlib

from log_io import _is_header, _split_row

from .constants import IMG_HASH_FILE


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load_img_hashes() -> tuple[dict[str, str], set[str]]:
    """통합 이미지 해시 캐시(`image_hashes.csv`)를 로드한다.

    Returns:
        (img_hashes, thumb_hashes):
            img_hashes   – dict[sha256_hex, rel_path]  모든 이미지의 해시→경로
            thumb_hashes – set[sha256_hex]  썸네일(og_image)인 해시 집합

    캐시 파일이 없으면 빈 값을 반환한다 — 다운로드 파이프라인이 `_img_hash_buf` 로
    `image_hashes.csv` 를 점증 기록하므로 별도 일괄 빌드는 두지 않는다.
    """
    img_hashes: dict[str, str] = {}
    thumb_hashes: set[str] = set()

    if not IMG_HASH_FILE.exists():
        return img_hashes, thumb_hashes

    for line in IMG_HASH_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or _is_header(line, "hash,relative_path,is_thumb"):
            continue
        parts = _split_row(line)
        if len(parts) >= 2:
            h = parts[0]
            rel = parts[1]
            is_thumb = parts[2] == "T" if len(parts) >= 3 else False
            if h and rel:
                img_hashes[h] = rel
                if is_thumb:
                    thumb_hashes.add(h)
    return img_hashes, thumb_hashes
