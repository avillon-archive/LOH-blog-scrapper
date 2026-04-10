# -*- coding: utf-8 -*-
"""run_media 진입점 — 포스트 단위 미디어 수집/다운로드."""

import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from pathlib import Path

from bs4 import BeautifulSoup

from log_io import csv_line
from utils import (
    DEFAULT_MAX_WORKERS,
    ROOT_DIR,
    date_to_folder,
    ensure_utf8_console,
    eta_str,
    extract_category,
    fetch_post_html,
    load_failed_post_urls,
    shutdown_event,
)

from download_images.url_utils import _clean_img_url

from .collect import collect_media_urls, is_forum_era_post
from .constants import (
    ANCHOR_APPEND,
    DONE_POSTS_MEDIA_FILE,
    FAILED_MEDIA_FILE,
    MEDIA_DIR,
    MEDIA_MAP_FILE,
    DONE_MEDIA_FILE,
)
from .download import download_one_media
from .persistence import (
    load_done_posts_media,
    load_media_url_to_path,
    load_seen_media,
    record_failed_media,
    remove_from_failed_media,
)
from .state import (
    _done_posts_media_buf,
    _media_done_buf,
    _media_map_buf,
)
from .wayback_discover import discover_forum_media


class MediaResult:
    __slots__ = ("ok", "saved", "fail", "post_fetch_ok", "succeeded_urls")

    def __init__(
        self,
        ok: int = 0,
        saved: int = 0,
        fail: int = 0,
        post_fetch_ok: bool = True,
        succeeded_urls: list[str] | None = None,
    ) -> None:
        self.ok = ok
        self.saved = saved
        self.fail = fail
        self.post_fetch_ok = post_fetch_ok
        self.succeeded_urls = succeeded_urls or []


def _process_post_media(
    post_url: str,
    post_date: str,
    seen_urls: set[str],
    media_map: dict[str, str],
    done_post_urls: dict[str, int],
    html_index: "dict[str, Path] | None" = None,
    *,
    retry_mode: bool = False,
    published_time: str = "",
) -> MediaResult:
    if post_url in done_post_urls and not retry_mode:
        return MediaResult(post_fetch_ok=True)

    html_text = fetch_post_html(post_url, html_index)
    if html_text is None:
        record_failed_media(post_url, "", "fetch_post_failed")
        return MediaResult(fail=1, post_fetch_ok=False)

    soup = BeautifulSoup(html_text, "lxml")

    # ── 수집 단계 ─────────────────────────────────────────────────────
    items = collect_media_urls(soup, post_url)
    inline_keys = {_clean_img_url(url) for url, *_ in items}

    # Cat C: 포럼 시대 포스트면 Wayback 포럼 스냅샷도 스캔
    if is_forum_era_post(soup):
        post_soup_cache: dict = {}
        for wb_item in discover_forum_media(
            post_url, post_soup_cache,
            blog_soup=soup, published_time=published_time,
        ):
            wb_url = wb_item[0]
            if _clean_img_url(wb_url) in inline_keys:
                continue
            items.append(wb_item)
            inline_keys.add(_clean_img_url(wb_url))

    if not items:
        done_post_urls[post_url] = 0
        _done_posts_media_buf.add(csv_line(post_url, "0"))
        return MediaResult(post_fetch_ok=True)

    category = extract_category(soup)
    folder = MEDIA_DIR / (category or "etc") / date_to_folder(post_date)

    ok = saved = fail = 0
    succeeded: list[str] = []
    for idx, (media_url, mtype, anchor_type, anchor_text) in enumerate(items, start=1):
        how = download_one_media(
            media_url, mtype, post_url, folder, idx,
            seen_urls, media_map,
            anchor_type=anchor_type, anchor_text=anchor_text,
        )
        if how:
            ok += 1
            succeeded.append(media_url)
            if how == "original":
                saved += 1
        else:
            record_failed_media(post_url, media_url, "download_failed")
            fail += 1

    if fail == 0:
        done_post_urls[post_url] = len(items)
        _done_posts_media_buf.add(csv_line(post_url, str(len(items))))

    return MediaResult(ok=ok, saved=saved, fail=fail, post_fetch_ok=True,
                       succeeded_urls=succeeded)


def run_media(
    posts: list[tuple[str, str, str]],
    retry_mode: bool = False,
    force_download: bool = False,
    html_index: "dict[str, Path] | None" = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> None:
    ensure_utf8_console()
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    seen_urls = set() if force_download else load_seen_media(DONE_MEDIA_FILE)
    media_map = load_media_url_to_path(MEDIA_MAP_FILE)
    done_post_urls: dict[str, int] = (
        {} if (force_download or retry_mode) else load_done_posts_media(DONE_POSTS_MEDIA_FILE)
    )

    if retry_mode:
        if not FAILED_MEDIA_FILE.exists():
            print("[미디어] 실패 파일이 없습니다.")
            return
        fail_posts = load_failed_post_urls(FAILED_MEDIA_FILE)
        posts = [(url, date, *rest) for url, date, *rest in posts if url in fail_posts]
        print(f"[미디어] 재처리 대상: {len(posts)}개 포스트")
        if not posts:
            print("[미디어] 재처리 대상이 없습니다.")
            return

    total = len(posts)
    if total == 0:
        print("[미디어] 처리할 포스트가 없습니다.")
        return

    report_interval = 10 if total <= 100 else 50
    start = time.time()
    total_ok = 0
    total_saved = 0
    total_fail = 0
    completed = 0
    counter_lock = threading.Lock()

    print(f"\n{'━' * 60}")
    print(f"[미디어] 다운로드 시작: {total}개 포스트")
    print(f"{'━' * 60}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_post = {
            executor.submit(
                _process_post_media, url, date, seen_urls, media_map,
                done_post_urls, html_index, retry_mode=retry_mode,
                published_time=pub_time,
            ): (url, date)
            for url, date, pub_time, *_ in posts
        }
        cancelled_count = 0
        for future in as_completed(future_to_post):
            post_url, _ = future_to_post[future]
            try:
                result = future.result()
            except CancelledError:
                cancelled_count += 1
                continue
            except Exception as exc:
                print(f"  [오류] {post_url}: {exc}")
                result = MediaResult(fail=1, post_fetch_ok=False)

            with counter_lock:
                total_ok += result.ok
                total_saved += result.saved
                total_fail += result.fail
                completed += 1
                cur_completed = completed

            if retry_mode and result.post_fetch_ok:
                remove_from_failed_media(post_url, reason="fetch_post_failed")
                for succeeded_url in result.succeeded_urls:
                    remove_from_failed_media(post_url, media_url=succeeded_url)

            if cur_completed % report_interval == 0 or cur_completed == total:
                eta = eta_str(cur_completed, total, start)
                existing = total_ok - total_saved
                print(f"  {eta} 저장={total_saved} 기존={existing} 실패={total_fail}")

            if shutdown_event.is_set():
                for f in future_to_post:
                    f.cancel()
                break

    _media_done_buf.flush_all()
    _media_map_buf.flush_all()
    _done_posts_media_buf.flush_all()

    existing = total_ok - total_saved
    if shutdown_event.is_set():
        print(
            f"\n[미디어 중단] 저장={total_saved} 기존={existing} "
            f"실패={total_fail} 취소={total - completed - cancelled_count}"
        )
    else:
        print(
            f"\n[미디어 완료] 저장={total_saved} 기존={existing} 실패={total_fail}"
        )
