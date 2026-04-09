# -*- coding: utf-8 -*-
"""공유 가변 상태: 락, LineBuffer, Wayback 캐시, _failed_log."""

import threading

from utils import LineBuffer

from .constants import (
    DONE_FILE,
    DONE_POSTS_FILE,
    FAILED_FILE,
    FALLBACK_DONE_FILE,
    FALLBACK_IMAGE_MAP_FILE,
    FALLBACK_IMG_HASH_FILE,
    FALLBACK_KAKAO_PF_LOG_FILE,
    FALLBACK_MULTILANG_LOG_FILE,
    IMAGE_MAP_FILE,
    IMG_HASH_FILE,
)
from .models import ImageFailedLog

# ---------------------------------------------------------------------------
# 스레드 안전을 위한 잠금
# ---------------------------------------------------------------------------

# ImageFailedLog 내부 캐시 전용 락
_dl_lock = threading.Lock()

# seen_urls / img_hashes / image_map 딕셔너리 갱신 전용 (in-memory, 극히 빠름)
_state_lock = threading.Lock()

# save_image() 파일명 충돌 해소 전용 (디스크 I/O 직렬화)
_save_lock = threading.Lock()

# ---------------------------------------------------------------------------
# 고빈도 파일용 LineBuffer
# ---------------------------------------------------------------------------

_done_buf = LineBuffer(DONE_FILE)  # downloaded_urls.txt — 제외 대상, 헤더 없음
_map_buf = LineBuffer(IMAGE_MAP_FILE, header="clean_url,relative_path")
_img_hash_buf = LineBuffer(IMG_HASH_FILE, header="sha256_hash,relative_path,is_thumbnail")
_done_posts_buf = LineBuffer(DONE_POSTS_FILE, header="post_url,image_count")

# ---------------------------------------------------------------------------
# Wayback CDX 캐시
# ---------------------------------------------------------------------------

_wayback_cache: dict[str, str | None] = {}
_wayback_events: dict[str, threading.Event] = {}
_wayback_cache_lock = threading.Lock()

# ---------------------------------------------------------------------------
# 이미지 실패 이력 싱글턴
# ---------------------------------------------------------------------------

_failed_log = ImageFailedLog(FAILED_FILE, _dl_lock)

# ---------------------------------------------------------------------------
# Fallback 전용 LineBuffer (--retry-fallback, primary 트래킹과 분리)
# ---------------------------------------------------------------------------

_fb_done_buf = LineBuffer(FALLBACK_DONE_FILE)  # fallback_downloaded_urls.txt — 제외 대상
_fb_map_buf = LineBuffer(FALLBACK_IMAGE_MAP_FILE, header="clean_url,relative_path")
_fb_img_hash_buf = LineBuffer(FALLBACK_IMG_HASH_FILE, header="sha256_hash,relative_path,is_thumbnail")
_fb_multilang_log_buf = LineBuffer(FALLBACK_MULTILANG_LOG_FILE, header="saved_path,post_url,source_url,original_img_url,phase")
_fb_kakao_pf_log_buf = LineBuffer(FALLBACK_KAKAO_PF_LOG_FILE, header="saved_path,post_url,source_url,original_img_url,phase")
