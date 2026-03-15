"""
run_all.py - master runner for image + markdown + html jobs

Usage:
  python run_all.py
  python run_all.py --images
  python run_all.py --md
  python run_all.py --html
  python run_all.py --retry
  python run_all.py --sample 10
  python run_all.py --sample 10 --seed 123
  python run_all.py --md --images --sample 10
  python run_all.py --posts
  python run_all.py --posts --images
  python run_all.py --pages
  python run_all.py --pages --md
  python run_all.py --custom
  python run_all.py --custom --images
"""

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from utils import (
    BLOG_RATE_LIMIT_SMALL,
    DEFAULT_MAX_WORKERS,
    build_html_index,
    ensure_utf8_console,
    load_failed_post_urls,
    load_posts,
    set_blog_rate_limit,
)
from build_posts_list import (
    build_and_write,
    build_pages_and_write,
    build_links_and_write,
    fetch_newest_sitemap_date,
    fetch_newest_single_sitemap_date,
    SITEMAP_URL,
    SITEMAP_PAGES_URL,
)
from download_images import run_images
from download_md import run_md
from download_html import run_html

ROOT_DIR = Path(__file__).parent / "loh_blog"
POSTS_FILE = ROOT_DIR / "all_posts.txt"
PAGES_FILE = ROOT_DIR / "all_pages.txt"
LINKS_FILE = ROOT_DIR / "all_links.txt"
CUSTOM_POSTS_FILE = ROOT_DIR / "custom_posts.txt"
FAILED_IMAGES_FILE = ROOT_DIR / "failed_images.txt"
FAILED_MD_FILE = ROOT_DIR / "failed_md.txt"
FAILED_HTML_FILE = ROOT_DIR / "failed_html.txt"
PIPELINE_ORDER = ("html", "images", "md")
HTML_DIR = ROOT_DIR / "html"
DONE_HTML_FILE = ROOT_DIR / "done_html.txt"


# ---------------------------------------------------------------------------
# all_links.txt 자동 갱신
# ---------------------------------------------------------------------------


def _newest_local_date(posts_file: Path) -> str:
    """파일의 첫 번째 유효 날짜(내림차순 최신)를 반환한다.

    파일이 없거나 읽기 실패 시 "" 반환.
    """
    try:
        for line in posts_file.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split("\t")
            if len(parts) == 2 and parts[1].strip():
                return parts[1].strip()
    except OSError:
        pass
    return ""


def _maybe_refresh_posts_list() -> None:
    """all_links.txt가 없거나 사이트맵(posts + pages) 최신 날짜와 불일치하면 재빌드한다."""
    local_date = _newest_local_date(LINKS_FILE)

    print("[포스트 목록] 사이트맵 최신 날짜 확인 중...")
    remote_date = fetch_newest_sitemap_date()

    if not remote_date:
        print("[포스트 목록] 사이트맵 날짜 확인 실패, 갱신 건너뜀")
        return

    if local_date and local_date == remote_date:
        print(f"[포스트 목록] 최신 상태 ({local_date}), 갱신 불필요")
        return

    if local_date:
        print(f"[포스트 목록] 갱신 필요 (로컬={local_date} → 사이트맵={remote_date})")
    else:
        print(f"[포스트 목록] all_links.txt 없음, 신규 생성 (사이트맵={remote_date})")

    # ── posts ──────────────────────────────────────────────────────────
    try:
        count_posts, _ = build_and_write()
        print(f"[포스트 목록] all_posts.txt 갱신 완료 ({count_posts}개 URL)")
    except Exception as e:
        print(f"[포스트 목록] all_posts.txt 갱신 실패: {e}")
        return

    # ── pages ──────────────────────────────────────────────────────────
    try:
        count_pages, _ = build_pages_and_write()
        print(f"[포스트 목록] all_pages.txt 갱신 완료 ({count_pages}개 URL)")
    except Exception as e:
        print(f"[포스트 목록] all_pages.txt 갱신 실패: {e}")
        # pages 실패해도 links 생성은 시도 (기존 all_pages.txt 있으면 활용 가능)

    # ── links (merge) ──────────────────────────────────────────────────
    try:
        count_links = build_links_and_write()
        print(f"[포스트 목록] all_links.txt 갱신 완료 ({count_links}개 URL, 최신={remote_date})")
    except Exception as e:
        print(f"[포스트 목록] all_links.txt 갱신 실패: {e}")


def _maybe_refresh_single(
    posts_file: Path, sitemap_url: str, build_fn, label: str
) -> None:
    """단일 사이트맵의 최신 날짜를 비교해 필요 시 파일을 갱신한다."""
    local_date = _newest_local_date(posts_file)

    print(f"[포스트 목록] {label} 사이트맵 최신 날짜 확인 중...")
    remote_date = fetch_newest_single_sitemap_date(sitemap_url)

    if not remote_date:
        print(f"[포스트 목록] {label} 사이트맵 날짜 확인 실패, 갱신 건너뜀")
        return

    if local_date and local_date == remote_date:
        print(f"[포스트 목록] {label} 최신 상태 ({local_date}), 갱신 불필요")
        return

    if local_date:
        print(f"[포스트 목록] {label} 갱신 필요 (로컬={local_date} → 사이트맵={remote_date})")
    else:
        print(f"[포스트 목록] {posts_file.name} 없음, 신규 생성 (사이트맵={remote_date})")

    try:
        count, _ = build_fn()
        print(f"[포스트 목록] {posts_file.name} 갱신 완료 ({count}개 URL)")
    except Exception as e:
        print(f"[포스트 목록] {posts_file.name} 갱신 실패: {e}")


def _load_failed_posts_for_retry(selected: set[str]) -> set[str]:
    """선택된 파이프라인 단계의 실패 목록을 합산해 반환한다."""
    targets: set[str] = set()
    if "images" in selected:
        targets |= load_failed_post_urls(FAILED_IMAGES_FILE)
    if "md" in selected:
        targets |= load_failed_post_urls(FAILED_MD_FILE)
    if "html" in selected:
        targets |= load_failed_post_urls(FAILED_HTML_FILE)
    return targets


def _sample_posts(
    posts: list[tuple[str, str]], n: int, seed: int | None = None
) -> list[tuple[str, str]]:
    if n >= len(posts):
        return list(posts)
    rng = random.Random(seed)
    return rng.sample(posts, n)


def _sample_source_label(selected: set[str]) -> str:
    if selected == {"images"}:
        return "failed_images.txt"
    if selected == {"md"}:
        return "failed_md.txt"
    if selected == {"html"}:
        return "failed_html.txt"

    labels: list[str] = []
    if "images" in selected:
        labels.append("failed_images.txt")
    if "md" in selected:
        labels.append("failed_md.txt")
    if "html" in selected:
        labels.append("failed_html.txt")
    return "union(" + " + ".join(labels) + ")"


def _count_file_lines(posts_file: Path) -> int:
    """파일의 유효 행 수(공백·# 주석 제외)를 반환한다.

    파일이 없거나 읽기 실패 시 0 반환.
    """
    try:
        count = 0
        for line in posts_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                count += 1
        return count
    except Exception:
        return 0


def main():
    ensure_utf8_console()
    parser = argparse.ArgumentParser(
        description="로드 오브 히어로즈 블로그 스크래퍼",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--images", action="store_true", help="이미지만 처리")
    parser.add_argument("--md", action="store_true", help="MD만 처리")
    parser.add_argument("--html", action="store_true", help="HTML만 처리")
    parser.add_argument("--retry", action="store_true", help="실패 목록 재처리")
    parser.add_argument("--retry-multilang", action="store_true",
                        help="KakaoPF 성공 이미지에 multilang alt 보충")
    parser.add_argument("--retry-kakaopf", action="store_true",
                        help="multilang 성공 이미지에 KakaoPF alt 보충")
    parser.add_argument(
        "--posts",
        action="store_true",
        help="all_posts.txt를 포스트 소스로 사용 (사이트맵 자동 갱신 건너뜀)",
    )
    parser.add_argument(
        "--pages",
        action="store_true",
        help="all_pages.txt를 포스트 소스로 사용 (사이트맵 자동 갱신 건너뜀)",
    )
    parser.add_argument(
        "--custom",
        action="store_true",
        help="custom_posts.txt를 포스트 소스로 사용 (사이트맵 자동 갱신 건너뜀)",
    )
    parser.add_argument("--sample", type=int, help="테스트용 랜덤 샘플 개수 (all_links.txt 행 수의 10%% 상한 적용)")
    parser.add_argument("--seed", type=int, help="샘플링 고정 시드(선택)")
    args = parser.parse_args()

    # ── 인수 유효성 검사 ────────────────────────────────────────────────
    if args.sample is not None and args.sample <= 0:
        parser.error("--sample must be a positive integer")

    source_flags = [args.posts, args.pages, args.custom]
    if sum(bool(f) for f in source_flags) > 1:
        parser.error("--posts, --pages, --custom 은 동시에 사용할 수 없습니다")

    if (args.posts or args.pages or args.custom) and args.sample is not None:
        parser.error("--posts / --pages / --custom 과 --sample 은 동시에 사용할 수 없습니다")

    # ── 파이프라인 단계 결정 ────────────────────────────────────────────
    selected = {
        name
        for name, enabled in (
            ("images", args.images),
            ("md", args.md),
            ("html", args.html),
        )
        if enabled
    }
    if not selected:
        selected = set(PIPELINE_ORDER)
    selected.add("html")  # html 은 항상 실행 (md/images 가 저장된 HTML 을 재활용)

    # ── 포스트 소스 파일 결정 ───────────────────────────────────────────
    force_download = False
    if args.posts:
        posts_file = POSTS_FILE
        source_label = "all_posts.txt"
    elif args.pages:
        posts_file = PAGES_FILE
        source_label = "all_pages.txt"
    elif args.custom:
        posts_file = CUSTOM_POSTS_FILE
        source_label = "custom_posts.txt"
        force_download = True
    else:
        posts_file = LINKS_FILE
        source_label = "all_links.txt"

    # ── 사이트맵 갱신 ─────────────────────────────────────────────────
    if args.posts:
        _maybe_refresh_single(POSTS_FILE, SITEMAP_URL, build_and_write, "posts")
    elif args.pages:
        _maybe_refresh_single(PAGES_FILE, SITEMAP_PAGES_URL, build_pages_and_write, "pages")
    elif args.custom:
        print(f"[포스트 목록] {source_label} 사용, 사이트맵 갱신 건너뜀 (강제 재다운로드)")
    else:
        _maybe_refresh_posts_list()
    print()

    # ── 포스트 로드 ────────────────────────────────────────────────────
    posts = load_posts(posts_file)
    if not posts:
        print(f"[오류] 포스트 목록을 불러오지 못했습니다: {posts_file}")
        sys.exit(1)

    # ── --sample 처리 ──────────────────────────────────────────────────
    sample_pool_label = source_label
    if args.sample is not None and args.retry:
        failed_pool = _load_failed_posts_for_retry(selected)
        posts = [(url, date) for url, date in posts if url in failed_pool]
        sample_pool_label = _sample_source_label(selected)
        print(
            f"[샘플] retry 실패 대상 풀에서 선택: source={sample_pool_label}, pool={len(posts)}"
        )
        if not posts:
            print("[종료] retry 샘플링 후보가 0개입니다. 실패 목록 파일을 확인하세요.")
            return

    if args.sample is not None:
        # all_links.txt 행 수 기준 10% 상한 적용
        all_count = _count_file_lines(LINKS_FILE)
        if all_count > 0:
            cap = max(1, all_count // 10)
            if args.sample > cap:
                print(
                    f"[샘플] --sample {args.sample} → 상한 적용 → {cap}"
                    f" (all_links.txt {all_count}행의 10%)"
                )
                args.sample = cap

        before = len(posts)
        posts = _sample_posts(posts, args.sample, seed=args.seed)
        print(
            f"[샘플] 요청={args.sample}, 실제={len(posts)}, 후보={before}, "
            f"source={sample_pool_label}, seed={args.seed}"
        )

    selected_order = [name for name in PIPELINE_ORDER if name in selected]
    print(
        f"[시작] 총 {len(posts)}개 포스트 | "
        f"소스={source_label} | "
        f"작업={','.join(selected_order)} | "
        f"재처리={'O' if args.retry else 'X'} | "
        f"샘플={args.sample if args.sample is not None else 'X'}"
    )
    print(f"[순서] {' -> '.join(selected_order)}")
    print()

    # ── 동적 워커 수·rate limit 설정 ──────────────────────────────────
    if args.retry:
        retry_urls: set[str] = set()
        for fpath in (FAILED_HTML_FILE, FAILED_IMAGES_FILE, FAILED_MD_FILE):
            retry_urls |= load_failed_post_urls(fpath)
        post_urls = {url for url, _ in posts}
        effective_count = len(retry_urls & post_urls) or len(posts)
    else:
        effective_count = len(posts)

    max_workers = min(effective_count, 32) if effective_count <= 100 else DEFAULT_MAX_WORKERS
    if effective_count <= 100:
        set_blog_rate_limit(BLOG_RATE_LIMIT_SMALL)  # 20 req/s
    print(f"[설정] workers={max_workers}, rate_limit={'20' if effective_count <= 100 else '10'} req/s")
    print()

    total_start = time.time()
    html_index: dict[str, "Path"] | None = None

    for step in selected_order:
        print("━" * 60)
        if step == "html":
            print("▶ HTML 파일 저장 시작")
            print("━" * 60)
            run_html(posts, retry_mode=args.retry, force_download=force_download,
                     max_workers=max_workers)
            html_index = build_html_index(HTML_DIR, DONE_HTML_FILE)
        elif step == "images":
            print("▶ 이미지 다운로드 시작")
            print("━" * 60)
            run_images(posts, retry_mode=args.retry,
                       retry_multilang=args.retry_multilang,
                       retry_kakaopf=args.retry_kakaopf,
                       force_download=force_download,
                       html_index=html_index, max_workers=max_workers)
        elif step == "md":
            print("▶ MD 파일 저장 시작")
            print("━" * 60)
            run_md(posts, retry_mode=args.retry, force_download=force_download,
                   html_index=html_index, max_workers=max_workers)
        print()

    elapsed = time.time() - total_start
    h = int(elapsed // 3600)
    m = int((elapsed % 3600) // 60)
    s = int(elapsed % 60)
    print(f"[전체 완료] 소요 시간: {h:02d}:{m:02d}:{s:02d}")


if __name__ == "__main__":
    main()
