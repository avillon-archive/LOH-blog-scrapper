# -*- coding: utf-8 -*-
"""download_media 경로·확장자 상수."""

from config import (  # noqa: F401 — re-export
    AUDIO_EXTS,
    COMMUNITY_CDN_HOST,
    COMMUNITY_SITE_HOST,
    IMG_EXTS,
    MEDIA_EXTS,
    NON_IMAGE_CONTEXT_KEYWORDS,
    ROOT_DIR,
    VIDEO_EXTS,
    is_gdrive_host,
)

MEDIA_DIR = ROOT_DIR / "media"
MEDIA_MAP_FILE = ROOT_DIR / "media_map.csv"
DONE_MEDIA_FILE = ROOT_DIR / "downloaded_media_urls.txt"
FAILED_MEDIA_FILE = ROOT_DIR / "failed_media.csv"
DONE_POSTS_MEDIA_FILE = ROOT_DIR / "done_posts_media.csv"

MEDIA_MAP_HEADER = "post_url,media_url,relative_path,anchor_type,anchor_text"
FAILED_MEDIA_HEADER = "post_url,media_url,reason"
DONE_POSTS_MEDIA_HEADER = "post_url,media_count"

# anchor_type 값
ANCHOR_INLINE = "inline"
ANCHOR_POSITIONED = "positioned"
ANCHOR_APPEND = "append"

# 앵커 텍스트 추출 파라미터
ANCHOR_MIN_LEN = 20
ANCHOR_MAX_LEN = 120
