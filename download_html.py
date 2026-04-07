"""HTML downloader for Lord of Heroes blog posts."""

from pathlib import Path
import threading

from bs4 import BeautifulSoup

from utils import (
    DEFAULT_MAX_WORKERS,
    ROOT_DIR,
    FailedLog,
    ensure_utf8_console,
    extract_category,
    fetch_with_retry,
    load_done_file,
    load_posts,
    run_pipeline,
    url_to_slug,
    write_text_unique,
)
HTML_DIR = ROOT_DIR / "html"
DONE_FILE = ROOT_DIR / "done_html.txt"
FAILED_FILE = ROOT_DIR / "failed_html.txt"


# ---------------------------------------------------------------------------
# 포스트 단위 처리 (스레드 안전)
# ---------------------------------------------------------------------------


def _process_post(
    post_url: str,
    done_slugs: dict[str, str],
    done_urls: set[str],
    html_dir: Path,
    done_file: Path,
    done_lock: threading.Lock,
    failed_log: FailedLog,
    force_overwrite: bool = False,
) -> bool:
    # 빠른 비잠금 확인
    if post_url in done_urls:
        return True

    resp = fetch_with_retry(post_url)
    if resp is None:
        failed_log.record(post_url, "fetch_post_failed")
        return False

    content_type = resp.headers.get("Content-Type", "")
    if "text/html" not in content_type.lower():
        failed_log.record(
            post_url,
            f"unexpected_content_type:{content_type.split(';')[0].strip()}",
        )
        return False

    html_text = resp.text
    slug = url_to_slug(post_url)

    # 카테고리 추출 → 저장 경로 결정
    soup = BeautifulSoup(html_text, "lxml")
    category = extract_category(soup)
    target_dir = html_dir / category if category else html_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        write_text_unique(
            target_dir, slug, ".html", html_text,
            done_slugs, done_urls, post_url,
            done_lock, done_file,
            force_overwrite=force_overwrite,
        )
    except OSError as e:
        failed_log.record(post_url, f"write_failed:{e}")
        return False
    return True


# ---------------------------------------------------------------------------
# 하위 호환: 기존 process_post(post_url, done_slugs, done_urls, force_overwrite)
# ---------------------------------------------------------------------------

# 모듈 레벨 락·FailedLog — 기존 외부 호출과의 하위 호환용
_html_done_lock = threading.Lock()
_html_fail_lock = threading.Lock()
_failed_log = FailedLog(FAILED_FILE, _html_fail_lock)


def process_post(
    post_url: str,
    done_slugs: dict[str, str],
    done_urls: set[str],
    force_overwrite: bool = False,
) -> bool:
    """하위 호환 래퍼 — 기본 KO 경로 사용."""
    return _process_post(
        post_url, done_slugs, done_urls,
        HTML_DIR, DONE_FILE, _html_done_lock, _failed_log,
        force_overwrite=force_overwrite,
    )


# ---------------------------------------------------------------------------
# 실행 진입점
# ---------------------------------------------------------------------------


def run_html(
    posts: list[tuple[str, ...]],
    retry_mode: bool = False,
    force_download: bool = False,
    max_workers: int = DEFAULT_MAX_WORKERS,
    html_dir: Path = HTML_DIR,
    done_file: Path = DONE_FILE,
    failed_file: Path = FAILED_FILE,
) -> None:
    ensure_utf8_console()
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    html_dir.mkdir(parents=True, exist_ok=True)
    done_slugs = load_done_file(done_file)
    done_urls: set[str] = set() if force_download else set(done_slugs.values())

    if not retry_mode:
        pending = sum(1 for url, *_ in posts if url not in done_urls)
        if pending == 0:
            print(f"[HTML] {len(posts)}개 포스트 모두 다운로드 완료, 건너뜀")
            return

    done_lock = threading.Lock()
    fail_lock = threading.Lock()
    failed_log = FailedLog(failed_file, fail_lock)

    process_fn = lambda url, date: _process_post(
        url, done_slugs, done_urls,
        html_dir, done_file, done_lock, failed_log,
        force_overwrite=force_download,
    )

    run_pipeline(
        posts,
        process_fn,
        failed_log,
        retry_mode,
        label="HTML",
        max_workers=max_workers,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="HTML 파일 저장")
    parser.add_argument("--retry", action="store_true", help="실패 목록 재처리")
    parser.add_argument("--posts", default=str(ROOT_DIR / "all_posts.txt"), help="포스트 목록 파일")
    args = parser.parse_args()

    posts = load_posts(args.posts)
    run_html(posts, retry_mode=args.retry)
