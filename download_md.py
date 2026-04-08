"""Markdown exporter for Lord of Heroes blog posts."""

from io import BytesIO
from pathlib import Path
import re
import threading
import urllib.parse

from bs4 import BeautifulSoup, Tag
from markitdown import MarkItDown

from utils import (
    DEFAULT_MAX_WORKERS,
    ROOT_DIR,
    FailedLog,
    LineBuffer,
    clean_url,
    ensure_utf8_console,
    extract_category,
    fetch_post_html,
    load_done_file,
    load_image_map,
    load_posts,
    load_stale,
    run_pipeline,
    url_to_slug,
    write_text_unique,
)
MD_DIR = ROOT_DIR / "md"
DONE_FILE = ROOT_DIR / "done_md.txt"
FAILED_FILE = ROOT_DIR / "failed_md.txt"
IMAGE_MAP_FILE = ROOT_DIR / "image_map.tsv"
STALE_FILE = ROOT_DIR / "stale_md.txt"

# post_to_md에서 사용하는 사전 컴파일 정규식
_TITLE_CLASS_RE = re.compile(r"post-title|article-title", re.I)
_BODY_CLASS_RE = re.compile(r"post-content|article-body|gh-content", re.I)
_UNWANTED_CLASS_RE = re.compile(
    r"post-share|post-tags|post-nav|related-posts|comments", re.I
)

# done_map / done_urls 갱신을 원자적으로 처리
_md_done_lock = threading.Lock()
# FailedLog 내부 캐시 보호 전용 (done 락과 분리해 불필요한 경합 방지)
_md_fail_lock = threading.Lock()

# 모듈 레벨 FailedLog 인스턴스 (utils.FailedLog 로 공통화)
_failed_log = FailedLog(FAILED_FILE, _md_fail_lock)

# 스레드별 MarkItDown 인스턴스 캐싱 (ONNX 모델 로딩 비용 절감)
_thread_local = threading.local()


def _get_converter() -> MarkItDown:
    if not hasattr(_thread_local, "converter"):
        _thread_local.converter = MarkItDown()
    return _thread_local.converter


# ---------------------------------------------------------------------------
# HTML 전처리 헬퍼
# ---------------------------------------------------------------------------


def _rewrite_images(
    body: Tag,
    post_url: str,
    image_map: dict[str, str],
    img_prefix: str,
) -> set[str]:
    """<img> src 속성을 image_map 기반 로컬 상대경로로 치환한다.

    Returns:
        image_map에 없어서 절대 URL로 남은 clean_url 집합.
    """
    unmapped: set[str] = set()
    for img in body.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        abs_src = urllib.parse.urljoin(post_url, src) if post_url else src
        key = clean_url(abs_src)
        relative_path = image_map.get(key)
        if relative_path:
            img["src"] = f"{img_prefix}{relative_path}"
        else:
            img["src"] = abs_src
            if abs_src.startswith("http"):
                unmapped.add(key)
        if img.get("data-src"):
            del img["data-src"]
    return unmapped


def _resolve_links(body: Tag, post_url: str) -> None:
    """상대 <a> href를 절대 URL로 변환한다."""
    for a in body.find_all("a", href=True):
        href = a["href"]
        if not href or href.startswith("#") or href.lower().startswith(("mailto:", "javascript:")):
            continue
        a["href"] = urllib.parse.urljoin(post_url, href)


def _flatten_nested_inline(body: Tag) -> None:
    """중첩된 동일 인라인 태그(<strong><strong>...</strong></strong> 등)를 평탄화한다.

    원본 HTML에 중첩 오류가 있으면 markitdown이 **********text********** 처럼
    마커를 누적 출력하므로, 변환 전에 불필요한 래핑을 제거한다.
    """
    for tag_name in ("strong", "b", "em", "i", "del", "s", "strike"):
        # 가장 안쪽부터 처리하기 위해 반복
        changed = True
        while changed:
            changed = False
            for tag in body.find_all(tag_name):
                parent = tag.parent
                if parent is None:
                    continue
                # 부모가 동일 인라인 태그이면 내부 태그는 항상 중복 — unwrap
                if parent.name == tag_name:
                    tag.unwrap()
                    changed = True
                    break


# ---------------------------------------------------------------------------
# Markdown 변환
# ---------------------------------------------------------------------------


def post_to_md(
    soup: BeautifulSoup,
    post_url: str,
    post_date: str,
    image_map: dict[str, str],
    category: str = "",
    img_prefix: str = "../",
) -> tuple[str, set[str]]:
    # ── 날짜 추출 (HTML 우선, 인자 fallback) ────────────────────────────
    date = ""
    og_date = soup.find("meta", property="article:published_time")
    if og_date and og_date.get("content"):
        # "2022-09-13T01:05:00.000Z" → "2022-09-13"
        date = og_date["content"][:10]
    if not date:
        date = post_date

    # ── 제목 추출 ──────────────────────────────────────────────────────
    title_tag = soup.find("h1", class_=_TITLE_CLASS_RE)
    if title_tag is None:
        title_tag = soup.find("h1")

    title = title_tag.get_text(strip=True) if title_tag else ""
    if not title:
        og_title = soup.find("meta", property="og:title")
        title = og_title["content"] if og_title and og_title.get("content") else ""

    # ── body 요소 찾기 ─────────────────────────────────────────────────
    body = (
        soup.find("section", class_=_BODY_CLASS_RE)
        or soup.find("div", class_=_BODY_CLASS_RE)
        or soup.find("article")
        or soup.find("main")
    )
    if body is None:
        body = soup.find("body") or soup

    # ── 불필요 요소 제거 ───────────────────────────────────────────────
    for author_card in body.find_all("div", class_="author-card"):
        author_card.decompose()

    for unwanted in body.find_all(class_=_UNWANTED_CLASS_RE):
        unwanted.decompose()

    if title:
        for h1 in body.find_all("h1"):
            if h1.get_text(strip=True) == title:
                h1.decompose()

    if title_tag and title_tag.parent is not None:
        title_tag.decompose()

    # ── HTML 전처리 ──────────────────────────────────────────────────
    _flatten_nested_inline(body)
    unmapped = _rewrite_images(body, post_url, image_map, img_prefix)
    _resolve_links(body, post_url)

    # ── markitdown으로 body 변환 ───────────────────────────────────────
    body_html = body.decode_contents()
    converter = _get_converter()
    result = converter.convert_stream(
        BytesIO(body_html.encode("utf-8")),
        file_extension=".html",
    )
    body_md = result.text_content.strip()

    # ── 메타데이터 헤더 + 본문 조합 ───────────────────────────────────
    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
        parts.append("")
    if date:
        parts.append(f"**작성일:** {date}")
    if category:
        parts.append(f"**카테고리:** {category}")
    parts.append(f"**원문:** {post_url}")
    parts.append("")
    parts.append("---")
    parts.append("")
    parts.append(body_md)

    return "\n".join(parts) + "\n", unmapped


# ---------------------------------------------------------------------------
# 포스트 단위 처리 (스레드 안전)
# ---------------------------------------------------------------------------


def process_post(
    post_url: str,
    post_date: str,
    done_slugs: dict[str, str],
    done_urls: set[str],
    image_map: dict[str, str],
    force_overwrite: bool = False,
    html_index: "dict[str, Path] | None" = None,
    stale_buf: "LineBuffer | None" = None,
) -> bool:
    # 빠른 비잠금 확인
    if post_url in done_urls:
        return True

    slug = url_to_slug(post_url)
    html_text = fetch_post_html(post_url, html_index)
    if html_text is None:
        _failed_log.record(post_url, "fetch_post_failed")
        return False

    soup = BeautifulSoup(html_text, "lxml")

    # 카테고리 추출 → 저장 경로 결정
    category = extract_category(soup)
    target_dir = MD_DIR / category if category else MD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    # target_dir의 ROOT_DIR 기준 depth로 img_prefix 자동 계산
    # md/ (depth=1) → "../"  /  md/카테고리/ (depth=2) → "../../"
    depth = len(target_dir.relative_to(ROOT_DIR).parts)
    img_prefix = "../" * depth

    md_text, unmapped = post_to_md(soup, post_url, post_date, image_map, category, img_prefix)

    # write_text_unique 가 slug 충돌 해소·쓰기·done 갱신을 일괄 처리한다.
    # None 반환은 already-done 을 의미하므로 성공으로 처리한다.
    try:
        write_text_unique(
            target_dir, slug, ".md", md_text,
            done_slugs, done_urls, post_url,
            _md_done_lock, DONE_FILE,
            force_overwrite=force_overwrite,
        )
    except OSError as e:
        _failed_log.record(post_url, f"write_failed:{e}")
        return False

    if stale_buf and unmapped:
        stale_buf.add(f"{post_url}\t{'|'.join(unmapped)}")

    return True


# ---------------------------------------------------------------------------
# 실행 진입점
# ---------------------------------------------------------------------------


def run_md(
    posts: list[tuple[str, str]],
    retry_mode: bool = False,
    force_download: bool = False,
    html_index: "dict[str, Path] | None" = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> None:
    ensure_utf8_console()
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    MD_DIR.mkdir(parents=True, exist_ok=True)
    image_map = load_image_map(IMAGE_MAP_FILE)
    done_slugs = load_done_file(DONE_FILE)
    done_urls: set[str] = set() if force_download else set(done_slugs.values())

    # ── stale 로드 + refresh 판정 ──────────────────────────────────────
    stale = load_stale(STALE_FILE)
    refresh_urls: set[str] = set()
    if not force_download:
        image_map_keys = image_map.keys()
        for url, unmapped in stale.items():
            if unmapped & image_map_keys:
                refresh_urls.add(url)
    if stale:
        if refresh_urls:
            done_urls -= refresh_urls
            for s in [s for s, u in done_slugs.items() if u in refresh_urls]:
                del done_slugs[s]
            print(f"[MD] stale {len(stale)}개 중 {len(refresh_urls)}개 포스트 재생성")
        else:
            print(f"[MD] stale {len(stale)}개 항목, refresh 대상 없음")

    # ── stale 파일 재작성 (처리 중 새로 기록) ──────────────────────────
    STALE_FILE.write_text("", encoding="utf-8")
    stale_buf = LineBuffer(STALE_FILE, flush_every=50)
    for url, unmapped in stale.items():
        if url not in refresh_urls:
            stale_buf.add(f"{url}\t{'|'.join(unmapped)}")

    process_fn = lambda url, date: process_post(
        url, date, done_slugs, done_urls, image_map,
        force_overwrite=force_download or url in refresh_urls,
        html_index=html_index,
        stale_buf=stale_buf,
    )

    run_pipeline(
        posts,
        process_fn,
        _failed_log,
        retry_mode,
        label="MD",
        max_workers=max_workers,
    )
    stale_buf.flush_all()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Markdown exporter")
    parser.add_argument("--retry", action="store_true", help="Retry failed list")
    parser.add_argument(
        "--posts",
        default=str(ROOT_DIR / "all_posts.txt"),
        help="Posts list file",
    )
    args = parser.parse_args()

    posts = load_posts(args.posts)
    run_md(posts, retry_mode=args.retry)
