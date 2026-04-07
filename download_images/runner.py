# -*- coding: utf-8 -*-
"""run_images 진입점 및 fallback CSV 생성."""

import csv
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils import (
    DEFAULT_MAX_WORKERS,
    ROOT_DIR,
    ensure_utf8_console,
    eta_str,
    load_failed_post_urls,
    load_image_map,
)

from .constants import (
    DONE_FILE,
    DONE_POSTS_FILE,
    FAILED_FILE,
    FALLBACK_REPORT_FILE,
    IMAGE_MAP_FILE,
    IMAGES_DIR,
    KAKAO_PF_LOG_FILE,
    MULTILANG_LOG_FILE,
)
from .fallback_kakao import KakaoPFPost, _build_kakao_pf_index
from .fallback_multilang import _build_multilang_date_index
from .hashing import _load_or_build_img_hashes
from .models import PostProcessResult
from .persistence import (
    _load_done_post_urls,
    load_seen,
    remove_from_failed,
    remove_from_failed_batch,
)
from .process import (
    _rename_fallback_images,
    _supplement_alt_images,
    process_post,
)
from .state import (
    _done_buf,
    _done_posts_buf,
    _img_hash_buf,
    _kakao_pf_log_buf,
    _map_buf,
    _multilang_log_buf,
)


def _generate_fallback_csv() -> int:
    """Kakao PF / 다국어 Wayback 폴백 로그를 읽어 CSV 리포트를 생성한다."""
    rows: list[list[str]] = []
    for log_file, fallback_type in [
        (KAKAO_PF_LOG_FILE, "kakao"),
        (MULTILANG_LOG_FILE, "multilang"),
    ]:
        if not log_file.exists():
            continue
        for line in log_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            saved_path = parts[0]
            post_url = parts[1]
            source_url = parts[2]
            original_img_url = parts[3] if len(parts) >= 4 else ""
            rows.append([post_url, original_img_url, fallback_type, saved_path, source_url])

    if not rows:
        return 0

    rows.sort(key=lambda r: (r[0], r[2], r[3]))
    with open(FALLBACK_REPORT_FILE, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["post_url", "original_img_url", "fallback_type", "saved_path", "source_url"])
        writer.writerows(rows)

    return len(rows)


def run_images(
    posts: list[tuple[str, str, str]],
    retry_mode: bool = False,
    retry_multilang: bool = False,
    retry_kakaopf: bool = False,
    force_download: bool = False,
    html_index: "dict[str, Path] | None" = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    fallback_disabled: bool = False,
):
    ensure_utf8_console()
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    fb_renamed = _rename_fallback_images()
    if fb_renamed:
        print(f"[이미지] 기존 폴백 이미지 {fb_renamed}개 rename (출처 접두사 추가)")

    if retry_multilang:
        _supplement_alt_images("multilang", posts, html_index, max_workers)
    if retry_kakaopf:
        _supplement_alt_images("kakaopf", posts, html_index, max_workers)
    if retry_multilang or retry_kakaopf:
        return

    seen_urls = set() if force_download else load_seen(DONE_FILE)
    img_hashes, thumb_hashes = _load_or_build_img_hashes()
    image_map = load_image_map(IMAGE_MAP_FILE)
    done_post_urls: dict[str, int] = {} if (force_download or retry_mode) else _load_done_post_urls(DONE_POSTS_FILE)

    if retry_mode:
        if not FAILED_FILE.exists():
            print("[이미지] 실패 파일이 없습니다.")
            return
        fail_posts = load_failed_post_urls(FAILED_FILE)
        posts = [(url, date, *rest) for url, date, *rest in posts if url in fail_posts]
        print(f"[이미지] 재처리 대상: {len(posts)}개 포스트")
        if not posts:
            print("[이미지] 재처리 대상이 없습니다.")
            return

    multilang_date_index: dict[str, list[tuple[str, str]]] = {}
    if retry_mode and not fallback_disabled:
        print("[이미지] 다국어 Wayback 폴백 활성화: EN/JA 사이트맵 인덱스 구축 중...")
        multilang_date_index = _build_multilang_date_index()

    kakao_pf_index: dict[str, list[KakaoPFPost]] = {}
    if retry_mode and not fallback_disabled:
        print("[이미지] Kakao PF 폴백 활성화: 게시글 인덱스 구축 중...")
        kakao_pf_index = _build_kakao_pf_index()
        if kakao_pf_index:
            total_kp = sum(len(v) for v in kakao_pf_index.values())
            print(f"  Kakao PF 인덱스: {len(kakao_pf_index)}일, 총 {total_kp}개 포스트")

    total = len(posts)
    report_interval = 10 if total <= 100 else 50
    start = time.time()
    total_ok = 0
    total_saved = 0
    total_fail = 0
    total_original = 0
    total_multilang = 0
    total_kakao = 0
    total_dedup = 0
    completed = 0
    counter_lock = threading.Lock()

    print(f"\n{'━' * 60}")
    print(f"[이미지] 다운로드 시작: {total}개 포스트")
    print(f"{'━' * 60}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_post = {
            executor.submit(
                process_post, url, date, seen_urls, img_hashes, image_map,
                thumb_hashes, done_post_urls,
                html_index=html_index,
                retry_mode=retry_mode,
                multilang_date_index=multilang_date_index,
                kakao_pf_index=kakao_pf_index,
                published_time=pub_time,
            ): (url, date)
            for url, date, pub_time, *_ in posts
        }
        for future in as_completed(future_to_post):
            post_url, _ = future_to_post[future]
            try:
                result = future.result()
            except Exception as exc:
                print(f"  [오류] {post_url}: {exc}")
                result = PostProcessResult(ok=0, fail=1, post_fetch_ok=False)

            with counter_lock:
                total_ok += result.ok
                total_saved += result.ok_saved
                total_fail += result.fail
                total_original += result.ok_original
                total_multilang += result.ok_multilang
                total_kakao += result.ok_kakao
                total_dedup += result.ok_dedup
                completed += 1
                cur_completed = completed

            if retry_mode and result.post_fetch_ok:
                remove_from_failed(post_url, reason="fetch_post_failed")
                if result.succeeded_urls:
                    remove_from_failed_batch(post_url, set(result.succeeded_urls))

            if cur_completed % report_interval == 0 or cur_completed == total:
                eta = eta_str(cur_completed, total, start)
                existing = total_ok - total_saved - total_dedup
                if retry_mode:
                    print(f"  {eta} 저장={total_saved} "
                          f"(원본={total_original} multilang={total_multilang} "
                          f"kakao={total_kakao}) 중복={total_dedup} "
                          f"기존={existing} 실패={total_fail}")
                else:
                    print(f"  {eta} 저장={total_saved} 중복={total_dedup} "
                          f"기존={existing} 실패={total_fail}")

    _done_buf.flush_all()
    _map_buf.flush_all()
    _img_hash_buf.flush_all()
    _done_posts_buf.flush_all()
    _multilang_log_buf.flush_all()
    _kakao_pf_log_buf.flush_all()

    if retry_mode:
        csv_count = _generate_fallback_csv()
        if csv_count:
            print(f"[이미지] 폴백 리포트: {FALLBACK_REPORT_FILE} ({csv_count}건)")

    existing = total_ok - total_saved - total_dedup
    if retry_mode:
        print(f"\n[이미지 완료] 저장={total_saved} "
              f"(원본={total_original} multilang={total_multilang} "
              f"kakao={total_kakao}) 중복={total_dedup} "
              f"기존={existing} 실패={total_fail}")
    else:
        print(f"\n[이미지 완료] 저장={total_saved} 중복={total_dedup} "
              f"기존={existing} 실패={total_fail}")
