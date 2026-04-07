# -*- coding: utf-8 -*-
"""공유 가변 상태: 락, LineBuffer, Wayback 캐시, _failed_log."""

import threading

from utils import LineBuffer

from .constants import (
    DONE_FILE,
    DONE_POSTS_FILE,
    FAILED_FILE,
    IMAGE_MAP_FILE,
    IMG_HASH_FILE,
    KAKAO_PF_LOG_FILE,
    MULTILANG_LOG_FILE,
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

_done_buf = LineBuffer(DONE_FILE)
_map_buf = LineBuffer(IMAGE_MAP_FILE)
_img_hash_buf = LineBuffer(IMG_HASH_FILE)
_done_posts_buf = LineBuffer(DONE_POSTS_FILE)
_multilang_log_buf = LineBuffer(MULTILANG_LOG_FILE)
_kakao_pf_log_buf = LineBuffer(KAKAO_PF_LOG_FILE)

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
