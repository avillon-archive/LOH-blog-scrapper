# -*- coding: utf-8 -*-
"""경로, 정규식, 상수 집합."""

import re

from config import (  # noqa: F401 — re-export for submodule consumers
    ARCHIVE_EXTS,
    BLOG_HOST,
    COMMUNITY_CDN_HOST,
    DL_KEYWORDS,
    DOWNLOADABLE_EXTS,
    EN_CAT_NORMALIZE,
    GAME_CDN_HOST,
    GDRIVE_HOSTS,
    IMG_EXTS,
    JA_CAT_NORMALIZE,
    IMAGE_OVERRIDES,
    KAKAO_PF_API,
    KAKAO_PF_PROFILE,
    KAKAO_TITLE_SIMILARITY,
    KO_TO_LANG_CAT,
    MULTILANG_BLOG_HOSTS,
    MULTILANG_EARLIEST_DATE,
    NON_IMAGE_CONTEXT_KEYWORDS as _NON_IMAGE_CONTEXT_KEYWORDS,
    ROOT_DIR,
    SKIP_LINK_HOSTS as _SKIP_LINK_HOSTS,
    WAYBACK_CDX_API,
)
from utils import SIZE_W_RE  # noqa: F401 – re-export

# ── ROOT_DIR 기반 경로 파생 ───────────────────────────────────────────────
IMAGES_DIR = ROOT_DIR / "images"
DONE_FILE = ROOT_DIR / "downloaded_urls.txt"
DONE_POSTS_FILE = ROOT_DIR / "done_posts_images.txt"
FAILED_FILE = ROOT_DIR / "failed_images.txt"
IMAGE_MAP_FILE = ROOT_DIR / "image_map.tsv"
THUMB_HASH_FILE = ROOT_DIR / "thumbnail_hashes.txt"
IMG_HASH_FILE = ROOT_DIR / "image_hashes.tsv"
MULTILANG_INDEX_CACHE = ROOT_DIR / "multilang_sitemap_index.json"  # 구 캐시 (삭제 대상)
MULTILANG_PUBLISHED_INDEX = ROOT_DIR / "multilang_published_index.json"
KAKAO_PF_INDEX_FILE = ROOT_DIR / "kakao_pf_index.json"
FALLBACK_REPORT_FILE = ROOT_DIR / "fallback_report.csv"

# Fallback 전용 (--retry-fallback, 보존 목적 — primary 트래킹과 완전 분리)
FALLBACK_IMAGES_DIR = ROOT_DIR / "images_fallback"
FALLBACK_DONE_FILE = ROOT_DIR / "fallback_downloaded_urls.txt"
FALLBACK_IMAGE_MAP_FILE = ROOT_DIR / "fallback_image_map.tsv"
FALLBACK_IMG_HASH_FILE = ROOT_DIR / "fallback_image_hashes.tsv"
FALLBACK_MULTILANG_LOG_FILE = ROOT_DIR / "fallback_multilang.tsv"
FALLBACK_KAKAO_PF_LOG_FILE = ROOT_DIR / "fallback_kakao_pf.tsv"
FALLBACK_STILL_FAILED_FILE = ROOT_DIR / "fallback_still_failed.tsv"

# ── 구현 수준 상수 (TOML 불필요) ──────────────────────────────────────────
RESOLUTION_RE = re.compile(r"\d+\s*[xX×]\s*\d+")
_KO_SUFFIX_RE = re.compile(r"(?i)_ko(?=[\._\-])")
_LANG_SUFFIX_MAP = {"en": "_EN", "ja": "_JP"}

_ARCHIVE_CONTENT_TYPES = {
    "application/zip", "application/x-zip-compressed",
    "application/x-rar-compressed", "application/x-7z-compressed",
    "application/gzip", "application/x-tar",
    "application/octet-stream",
}
