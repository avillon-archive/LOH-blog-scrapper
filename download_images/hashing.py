# -*- coding: utf-8 -*-
"""SHA-256 유틸 및 해시 캐시 로드/빌드."""

import hashlib
from pathlib import Path

from utils import ROOT_DIR

from .constants import (
    DOWNLOADABLE_EXTS,
    IMAGES_DIR,
    IMG_HASH_FILE,
    THUMB_HASH_FILE,
)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _build_hash_index(folder: Path) -> set[str]:
    hashes: set[str] = set()
    if not folder.exists():
        return hashes
    for file_path in folder.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            hashes.add(_sha256_bytes(file_path.read_bytes()))
        except OSError:
            continue
    return hashes


def _load_or_build_og_hashes() -> set[str]:
    """레거시: 썸네일 해시 set 로드 (마이그레이션용)."""
    if THUMB_HASH_FILE.exists():
        hashes = set(THUMB_HASH_FILE.read_text(encoding="utf-8").splitlines())
        hashes.discard("")
        return hashes
    return set()


def _load_or_build_img_hashes() -> tuple[dict[str, str], set[str]]:
    """통합 이미지 해시 캐시를 로드한다.

    Returns:
        (img_hashes, thumb_hashes):
            img_hashes  – dict[sha256_hex, rel_path]  모든 이미지의 해시→경로
            thumb_hashes – set[sha256_hex]  썸네일(og_image)인 해시 집합
    """
    img_hashes: dict[str, str] = {}
    thumb_hashes: set[str] = set()

    if IMG_HASH_FILE.exists():
        for line in IMG_HASH_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                h = parts[0].strip()
                rel = parts[1].strip()
                is_thumb = parts[2].strip() == "T" if len(parts) >= 3 else False
                if h and rel:
                    img_hashes[h] = rel
                    if is_thumb:
                        thumb_hashes.add(h)
        return img_hashes, thumb_hashes

    # 캐시 미존재 → 기존 이미지 폴더를 스캔해 빌드
    old_thumb_dir = (IMAGES_DIR / "thumbnails").resolve()
    legacy_thumb_hashes = _load_or_build_og_hashes()

    if IMAGES_DIR.exists():
        for file_path in IMAGES_DIR.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in DOWNLOADABLE_EXTS:
                continue
            try:
                h = _sha256_bytes(file_path.read_bytes())
                rel = file_path.relative_to(ROOT_DIR).as_posix()
                if h not in img_hashes:
                    img_hashes[h] = rel
                is_in_thumb_dir = old_thumb_dir in file_path.resolve().parents
                if is_in_thumb_dir or h in legacy_thumb_hashes:
                    thumb_hashes.add(h)
            except OSError:
                continue

    # 캐시 파일 작성
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{h}\t{rel}\t{'T' if h in thumb_hashes else ''}"
        for h, rel in img_hashes.items()
    ]
    IMG_HASH_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return img_hashes, thumb_hashes
