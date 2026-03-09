"""HTML downloader for Lord of Heroes blog posts."""

from pathlib import Path
import threading

from bs4 import BeautifulSoup

from utils import (
    DEFAULT_MAX_WORKERS,
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

ROOT_DIR = Path(__file__).parent / "loh_blog"
HTML_DIR = ROOT_DIR / "html"
DONE_FILE = ROOT_DIR / "done_html.txt"
FAILED_FILE = ROOT_DIR / "failed_html.txt"

# done_map / done_urls 갱신을 원자적으로 처리
_html_done_lock = threading.Lock()
# FailedLog 내부 캐시 보호 전용 (done 락과 분리해 불필요한 경합 방지)
_html_fail_lock = threading.Lock()

# 모듈 레벨 FailedLog 인스턴스 (utils.FailedLog 로 공통화)
_failed_log = FailedLog(FAILED_FILE, _html_fail_lock)


# ---------------------------------------------------------------------------
# 포스트 단위 처리 (스레드 안전)
# ---------------------------------------------------------------------------


def process_post(
    post_url: str,
    done_slugs: dict[str, str],
    done_urls: set[str],
) -> bool:
    # 빠른 비잠금 확인
    if post_url in done_urls:
        return True

    resp = fetch_with_retry(post_url)
    if resp is None:
        _failed_log.record(post_url, "fetch_post_failed")
        return False

    content_type = resp.headers.get("Content-Type", "")
    if "text/html" not in content_type.lower():
        _failed_log.record(
            post_url,
            f"unexpected_content_type:{content_type.split(';')[0].strip()}",
        )
        return False

    html_text = resp.text
    slug = url_to_slug(post_url)

    # 카테고리 추출 → 저장 경로 결정
    soup = BeautifulSoup(html_text, "lxml")
    category = extract_category(soup)
    target_dir = HTML_DIR / category if category else HTML_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    # write_text_unique 가 slug 충돌 해소·쓰기·done 갱신을 일괄 처리한다.
    # None 반환은 already-done 을 의미하므로 성공으로 처리한다.
    try:
        write_text_unique(
            target_dir, slug, ".html", html_text,
            done_slugs, done_urls, post_url,
            _html_done_lock, DONE_FILE,
        )
    except OSError as e:
        _failed_log.record(post_url, f"write_failed:{e}")
        return False
    return True


# ---------------------------------------------------------------------------
# 실행 진입점
# ---------------------------------------------------------------------------


def run_html(posts: list[tuple[str, str]], retry_mode: bool = False) -> None:
    ensure_utf8_console()
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    done_slugs = load_done_file(DONE_FILE)
    done_urls = set(done_slugs.values())

    # process_post는 date를 사용하지 않으므로 lambda로 시그니처를 맞춘다.
    process_fn = lambda url, date: process_post(url, done_slugs, done_urls)

    run_pipeline(
        posts,
        process_fn,
        _failed_log,
        retry_mode,
        label="HTML",
        max_workers=DEFAULT_MAX_WORKERS,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="HTML 파일 저장")
    parser.add_argument("--retry", action="store_true", help="실패 목록 재처리")
    parser.add_argument("--posts", default=str(ROOT_DIR / "all_posts.txt"), help="포스트 목록 파일")
    args = parser.parse_args()

    posts = load_posts(args.posts)
    run_html(posts, retry_mode=args.retry)
