# -*- coding: utf-8 -*-
"""download_media 공유 락·버퍼."""

import threading

from log_io import LineBuffer

from download_images.models import ImageFailedLog

from .constants import (
    DONE_MEDIA_FILE,
    DONE_POSTS_MEDIA_FILE,
    DONE_POSTS_MEDIA_HEADER,
    FAILED_MEDIA_FILE,
    MEDIA_MAP_FILE,
    MEDIA_MAP_HEADER,
)

# download_images.state 의 _wayback_cache / _wayback_events 를 직접 재사용한다
# (동일 URL 에 대한 CDX 조회를 파이프라인 간 공유).

_media_state_lock = threading.Lock()
_media_save_lock = threading.Lock()
_media_dl_lock = threading.Lock()

_media_done_buf = LineBuffer(DONE_MEDIA_FILE)
_media_map_buf = LineBuffer(MEDIA_MAP_FILE, header=MEDIA_MAP_HEADER)
_done_posts_media_buf = LineBuffer(DONE_POSTS_MEDIA_FILE, header=DONE_POSTS_MEDIA_HEADER)

# failed_media.csv 는 (post_url, media_url, reason) 3-tuple 이므로 ImageFailedLog 재사용
_failed_media_log = ImageFailedLog(FAILED_MEDIA_FILE, _media_dl_lock)
