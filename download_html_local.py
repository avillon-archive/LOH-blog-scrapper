# -*- coding: utf-8 -*-
"""오프라인 열람용 HTML 생성.

이미 다운로드된 html/ 파일을 기반으로:
  1. 이미지 경로를 image_map.tsv 기반 로컬 상대경로로 리라이트
  2. 모든 <script> 태그 제거
  3. CSS를 로컬에 저장하고 href를 상대경로로 교체
  4. html_local/ 에 저장
"""

import re
import threading
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from utils import (
    DEFAULT_MAX_WORKERS,
    ROOT_DIR,
    FailedLog,
    clean_url,
    ensure_utf8_console,
    extract_category,
    fetch_with_retry,
    load_image_map,
    run_pipeline,
)

HTML_DIR = ROOT_DIR / "html"
HTML_LOCAL_DIR = ROOT_DIR / "html_local"
ASSETS_DIR = HTML_LOCAL_DIR / "assets"
IMAGE_MAP_FILE = ROOT_DIR / "image_map.tsv"
DONE_FILE = ROOT_DIR / "done_html_local.txt"
FAILED_FILE = ROOT_DIR / "failed_html_local.txt"

# CSS url(...) 참조 패턴
CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""")
# 인라인 style background-image 패턴
BG_IMAGE_RE = re.compile(
    r"""(background(?:-image)?\s*:[^;]*url\(\s*['"]?)([^'")]+)(['"]?\s*\))""",
    re.IGNORECASE,
)
# 블로그 base URL
BLOG_BASE = "https://blog-ko.lordofheroes.com"


# ---------------------------------------------------------------------------
# CSS 다운로드 (1회)
# ---------------------------------------------------------------------------

_css_downloaded: set[str] = set()
_css_lock = threading.Lock()


def _download_css(css_url: str) -> str | None:
    """CSS 파일을 assets/ 에 다운로드하고 로컬 파일명을 반환한다.

    이미 다운로드된 URL 이면 파일명만 반환한다.
    """
    with _css_lock:
        if css_url in _css_downloaded:
            # 이미 다운로드됨 — 파일명만 반환
            filename = _css_filename(css_url)
            if (ASSETS_DIR / filename).exists():
                return filename
            # 파일이 없으면 재시도
        _css_downloaded.add(css_url)

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    filename = _css_filename(css_url)
    local_path = ASSETS_DIR / filename

    if local_path.exists():
        return filename

    resp = fetch_with_retry(css_url)
    if resp is None:
        return None

    css_text = resp.text

    # CSS 내 url() 참조 중 상대경로를 절대 URL로 변환
    # (폰트, 배경이미지 등 — 이들은 원본 URL 유지)
    base = css_url.rsplit("/", 1)[0] + "/"
    def _resolve_css_url(m: re.Match) -> str:
        ref = m.group(1)
        if ref.startswith(("data:", "http://", "https://", "//")):
            return m.group(0)
        absolute = urllib.parse.urljoin(base, ref)
        return f"url({absolute})"

    css_text = CSS_URL_RE.sub(_resolve_css_url, css_text)

    local_path.write_text(css_text, encoding="utf-8")
    return filename


def _css_filename(css_url: str) -> str:
    """CSS URL 에서 파일명을 추출한다 (쿼리 파라미터 제거)."""
    path = urllib.parse.urlparse(css_url).path
    return path.rsplit("/", 1)[-1] or "style.css"


# ---------------------------------------------------------------------------
# 이미지 경로 리라이트
# ---------------------------------------------------------------------------


def _img_prefix(category: str | None) -> str:
    """html_local 파일 위치에서 images/ 디렉토리까지의 상대경로 접두사."""
    # html_local/{category}/{slug}.html → ../../images/
    # html_local/{slug}.html            → ../images/
    if category:
        return "../../"
    return "../"


def _assets_prefix(category: str | None) -> str:
    """html_local 파일 위치에서 assets/ 디렉토리까지의 상대경로 접두사."""
    if category:
        return "../assets/"
    return "assets/"


def _rewrite_img_src(
    src: str,
    post_url: str,
    image_map: dict[str, str],
    prefix: str,
) -> tuple[str, bool]:
    """이미지 src를 로컬 경로로 변환 시도.

    Returns:
        (new_src, mapped) — mapped=True 이면 로컬 경로로 교체됨.
    """
    if not src or src.startswith("data:"):
        return src, False
    abs_src = urllib.parse.urljoin(post_url, src)
    key = clean_url(abs_src)
    relative_path = image_map.get(key)
    if relative_path:
        return f"{prefix}{relative_path}", True
    return abs_src, False


def _rewrite_images(
    soup: BeautifulSoup,
    post_url: str,
    image_map: dict[str, str],
    prefix: str,
) -> None:
    """<img> 태그의 src/srcset/data-src 를 로컬 경로로 리라이트."""
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        new_src, mapped = _rewrite_img_src(src, post_url, image_map, prefix)
        if mapped:
            img["src"] = new_src
            # 매핑 성공 시에만 srcset 제거, data-src 제거
            if img.get("srcset"):
                del img["srcset"]
            if img.get("data-src"):
                del img["data-src"]
        else:
            # 매핑 실패 — 원본 유지 (절대 URL 로 정규화만)
            img["src"] = new_src
            if img.get("data-src"):
                del img["data-src"]


def _rewrite_meta_images(
    soup: BeautifulSoup,
    post_url: str,
    image_map: dict[str, str],
    prefix: str,
) -> None:
    """og:image, twitter:image, favicon 등 메타 이미지 리라이트."""
    # og:image, twitter:image
    for meta in soup.find_all("meta", attrs={"content": True}):
        prop = meta.get("property", "") or meta.get("name", "")
        if "image" not in prop.lower():
            continue
        new_src, mapped = _rewrite_img_src(
            meta["content"], post_url, image_map, prefix
        )
        if mapped:
            meta["content"] = new_src

    # favicon
    for link in soup.find_all("link", rel=lambda v: v and "icon" in " ".join(v).lower()):
        href = link.get("href", "")
        new_src, mapped = _rewrite_img_src(href, post_url, image_map, prefix)
        if mapped:
            link["href"] = new_src

    # JSON-LD 내 이미지 URL — 문자열 치환으로 처리
    for script_tag in soup.find_all("script", type="application/ld+json"):
        if not script_tag.string:
            continue
        text = script_tag.string
        changed = False
        for match in re.finditer(r'"url"\s*:\s*"([^"]+)"', text):
            url_val = match.group(1)
            new_src, mapped = _rewrite_img_src(url_val, post_url, image_map, prefix)
            if mapped:
                text = text.replace(url_val, new_src)
                changed = True
        if changed:
            script_tag.string = text


def _rewrite_style_bg_images(
    soup: BeautifulSoup,
    post_url: str,
    image_map: dict[str, str],
    prefix: str,
) -> None:
    """<style> 블록과 인라인 style 속성의 background-image url() 리라이트."""
    # <style> 블록
    for style_tag in soup.find_all("style"):
        if not style_tag.string:
            continue
        style_tag.string = _rewrite_bg_urls(style_tag.string, post_url, image_map, prefix)

    # 인라인 style 속성
    for tag in soup.find_all(style=True):
        tag["style"] = _rewrite_bg_urls(tag["style"], post_url, image_map, prefix)


def _rewrite_bg_urls(
    css_text: str,
    post_url: str,
    image_map: dict[str, str],
    prefix: str,
) -> str:
    """CSS 텍스트 내 url() 참조를 image_map 기반으로 리라이트."""
    def _replace(m: re.Match) -> str:
        full = m.group(0)
        url_val = m.group(2)
        if url_val.startswith("data:"):
            return full
        abs_url = urllib.parse.urljoin(post_url, url_val)
        key = clean_url(abs_url)
        relative_path = image_map.get(key)
        if relative_path:
            return m.group(1) + prefix + relative_path + m.group(3)
        # 매핑 실패 — 절대 URL 로 정규화
        return m.group(1) + abs_url + m.group(3)

    return BG_IMAGE_RE.sub(_replace, css_text)


# ---------------------------------------------------------------------------
# 내부 링크 로컬 리라이트
# ---------------------------------------------------------------------------

# 블로그 내부 경로 패턴: /postXXX/, /pageXXX/, /slug/ 등
BLOG_HOST_RE = re.compile(
    r"^https?://blog-ko\.lordofheroes\.com(/.*)",
    re.IGNORECASE,
)


def _rewrite_internal_links(
    soup: BeautifulSoup,
    post_url: str,
    slug_map: dict[str, str],
    current_category: str | None,
) -> None:
    """블로그 내부 <a href> 를 로컬 html_local 파일 상대경로로 리라이트.

    Args:
        slug_map: {path_segment: category_relative_path}
                  예: {"post202009031200": "아발론서고/post202009031200.html"}
        current_category: 현재 파일의 카테고리 (None 이면 루트).
    """
    for a in soup.find_all("a", href=True):
        href = a["href"]

        # 절대 URL → 경로 추출
        m = BLOG_HOST_RE.match(href)
        if m:
            path = m.group(1)
        elif href.startswith("/"):
            path = href
        else:
            continue

        # 경로에서 slug 추출: /post202009031200/ → post202009031200
        segments = [s for s in path.split("/") if s]
        if not segments:
            continue
        # 태그 페이지, author 페이지 등은 건너뜀
        if segments[0] in ("tag", "author", "rss", "assets", "content", "public"):
            continue
        slug = segments[0]

        rel_path = slug_map.get(slug)
        if rel_path is None:
            continue

        # 현재 파일 위치 기준 상대경로 산출
        if current_category:
            # html_local/{category}/{file}.html → ../{rel_path}
            a["href"] = f"../{rel_path}"
        else:
            # html_local/{file}.html → {rel_path}
            a["href"] = rel_path


def _build_slug_map(
    source: list[tuple[Path, str]],
    html_dir: Path,
) -> dict[str, str]:
    """소스 HTML 파일 목록에서 {slug: category_relative_path} 매핑을 구축.

    예: html/공지사항/202009021723.html
      → {"202009021723": "공지사항/202009021723.html"}
    예: html/page12172.html
      → {"page12172": "page12172.html"}
    """
    slug_map: dict[str, str] = {}
    for html_path, url in source:
        slug = html_path.stem
        # html_dir 기준 상대 경로
        try:
            rel = html_path.relative_to(html_dir)
        except ValueError:
            rel = Path(f"{slug}.html")
        # 파일명을 .html 로 보장
        rel = rel.with_suffix(".html")
        slug_map[slug] = str(rel).replace("\\", "/")

        # URL 경로의 첫 세그먼트도 매핑 (slug와 다를 수 있음)
        parsed_path = urllib.parse.urlparse(url).path
        segments = [s for s in parsed_path.split("/") if s]
        if segments and segments[0] != slug:
            slug_map[segments[0]] = str(rel).replace("\\", "/")
    return slug_map


# ---------------------------------------------------------------------------
# JS 제거
# ---------------------------------------------------------------------------


def _remove_scripts(soup: BeautifulSoup) -> None:
    """모든 <script> 태그를 제거한다. <noscript> 는 유지."""
    for script in soup.find_all("script"):
        script.decompose()


# ---------------------------------------------------------------------------
# CSS 로컬화
# ---------------------------------------------------------------------------


def _localize_css(soup: BeautifulSoup, assets_pref: str) -> None:
    """외부 CSS <link> 를 로컬 경로로 교체한다."""
    for link in soup.find_all("link", rel="stylesheet"):
        href = link.get("href", "")
        if not href:
            continue
        filename = _download_css(href)
        if filename:
            link["href"] = f"{assets_pref}{filename}"


# ---------------------------------------------------------------------------
# 포스트 단위 처리
# ---------------------------------------------------------------------------


def _process_post(
    html_path: Path,
    post_url: str,
    image_map: dict[str, str],
    slug_map: dict[str, str],
    html_local_dir: Path,
    done_slugs: dict[str, str],
    done_urls: set[str],
    done_file: Path,
    done_lock: threading.Lock,
    failed_log: FailedLog,
    force_overwrite: bool = False,
) -> bool:
    if post_url in done_urls:
        return True

    try:
        html_text = html_path.read_text(encoding="utf-8")
    except OSError as e:
        failed_log.record(post_url, f"read_failed:{e}")
        return False

    soup = BeautifulSoup(html_text, "lxml")

    # 카테고리 결정
    category = extract_category(soup)
    target_dir = html_local_dir / category if category else html_local_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    prefix = _img_prefix(category)
    assets_pref = _assets_prefix(category)

    # 1. CSS 로컬화
    _localize_css(soup, assets_pref)

    # 2. 이미지 리라이트
    _rewrite_images(soup, post_url, image_map, prefix)
    _rewrite_meta_images(soup, post_url, image_map, prefix)
    _rewrite_style_bg_images(soup, post_url, image_map, prefix)

    # 3. 내부 링크 로컬화
    _rewrite_internal_links(soup, post_url, slug_map, category)

    # 4. JS 제거
    _remove_scripts(soup)

    # 5. 저장
    slug = html_path.stem
    output = str(soup)
    out_path = target_dir / f"{slug}.html"

    try:
        out_path.write_text(output, encoding="utf-8")
    except OSError as e:
        failed_log.record(post_url, f"write_failed:{e}")
        return False

    # done 기록
    with done_lock:
        done_slugs[slug] = post_url
        done_urls.add(post_url)
        with open(done_file, "a", encoding="utf-8") as f:
            f.write(f"{slug}\t{post_url}\n")

    return True


# ---------------------------------------------------------------------------
# 실행 진입점
# ---------------------------------------------------------------------------


def _load_done_file(filepath: Path) -> dict[str, str]:
    """done_html_local.txt → {slug: post_url}."""
    done: dict[str, str] = {}
    if not filepath.exists():
        return done
    for line in filepath.read_text(encoding="utf-8").splitlines():
        row = line.strip()
        if not row:
            continue
        parts = row.split("\t", 1)
        if len(parts) == 2:
            done[parts[0]] = parts[1]
    return done


def _build_html_file_index(
    html_dir: Path,
    done_html_file: Path,
) -> list[tuple[Path, str]]:
    """done_html.txt 기반으로 (html_path, post_url) 리스트를 구축한다."""
    from utils import load_done_file

    done_map = load_done_file(done_html_file)  # {slug: url}
    slug_to_path: dict[str, Path] = {}
    for html_file in html_dir.rglob("*.html"):
        slug_to_path[html_file.stem] = html_file

    result: list[tuple[Path, str]] = []
    for slug, url in done_map.items():
        if slug in slug_to_path:
            result.append((slug_to_path[slug], url))
    return result


def run_html_local(
    retry_mode: bool = False,
    force_download: bool = False,
    max_workers: int = DEFAULT_MAX_WORKERS,
    html_dir: Path = HTML_DIR,
    html_local_dir: Path = HTML_LOCAL_DIR,
    done_html_file: Path = ROOT_DIR / "done_html.txt",
    done_file: Path = DONE_FILE,
    failed_file: Path = FAILED_FILE,
) -> None:
    ensure_utf8_console()
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    html_local_dir.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    image_map = load_image_map(IMAGE_MAP_FILE)
    print(f"[HTML-LOCAL] image_map 로드: {len(image_map)}개 항목")

    # 소스: done_html.txt 기반 (html_path, post_url) 목록
    source = _build_html_file_index(html_dir, done_html_file)
    slug_map = _build_slug_map(source, html_dir)
    print(f"[HTML-LOCAL] slug_map 로드: {len(slug_map)}개 항목")
    if not source:
        print("[HTML-LOCAL] 소스 HTML 파일이 없습니다.")
        return

    # done 상태 로드
    done_slugs = _load_done_file(done_file)
    done_urls: set[str] = set() if force_download else set(done_slugs.values())

    # run_pipeline 용 (url, date) 형식으로 변환
    # html_path 를 전달하기 위해 path→url 매핑 유지
    url_to_path: dict[str, Path] = {url: path for path, url in source}
    posts = [(url, "") for _, url in source]

    if not retry_mode:
        pending = sum(1 for url, _ in posts if url not in done_urls)
        if pending == 0:
            print(f"[HTML-LOCAL] {len(posts)}개 포스트 모두 처리 완료, 건너뜀")
            return

    done_lock = threading.Lock()
    fail_lock = threading.Lock()
    failed_log = FailedLog(failed_file, fail_lock)

    def process_fn(url: str, date: str) -> bool:
        html_path = url_to_path.get(url)
        if html_path is None:
            failed_log.record(url, "source_html_not_found")
            return False
        return _process_post(
            html_path, url, image_map, slug_map,
            html_local_dir, done_slugs, done_urls,
            done_file, done_lock, failed_log,
            force_overwrite=force_download,
        )

    run_pipeline(
        posts,
        process_fn,
        failed_log,
        retry_mode,
        label="HTML-LOCAL",
        max_workers=max_workers,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="오프라인 열람용 HTML 생성")
    parser.add_argument("--retry", action="store_true", help="실패 목록 재처리")
    parser.add_argument("--force", action="store_true", help="기존 기록 무시하고 전체 재생성")
    args = parser.parse_args()

    run_html_local(retry_mode=args.retry, force_download=args.force)
