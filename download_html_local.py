# -*- coding: utf-8 -*-
"""오프라인 열람용 HTML 생성.

이미 다운로드된 html/ 파일을 기반으로:
  1. 이미지 경로를 image_map.tsv 기반 로컬 상대경로로 리라이트
  2. 모든 <script> 태그 제거
  3. CSS를 로컬에 저장하고 href를 상대경로로 교체
  4. html_local/ 에 저장
"""

import hashlib
import re
import threading
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup

from utils import (
    DEFAULT_MAX_WORKERS,
    ROOT_DIR,
    FailedLog,
    build_html_index,
    clean_url,
    ensure_utf8_console,
    extract_category,
    fetch_with_retry,
    load_done_file,
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

# ---------------------------------------------------------------------------
# CSS 다운로드
# ---------------------------------------------------------------------------


class CssDownloader:
    """CSS 파일을 assets/ 에 다운로드하고 캐싱. 스레드 안전."""

    def __init__(self, assets_dir: Path) -> None:
        self._assets_dir = assets_dir
        self._lock = threading.Lock()
        self._url_locks: dict[str, threading.Lock] = {}

    def download(self, css_url: str) -> str | None:
        """CSS 파일을 다운로드하고 로컬 파일명을 반환. 이미 있으면 파일명만 반환."""
        filename = self._filename(css_url)
        local_path = self._assets_dir / filename
        if local_path.exists():
            return filename

        # per-URL lock 획득 (같은 URL 동시 다운로드 방지)
        with self._lock:
            if css_url not in self._url_locks:
                self._url_locks[css_url] = threading.Lock()
            url_lock = self._url_locks[css_url]

        with url_lock:
            if local_path.exists():
                return filename
            resp = fetch_with_retry(css_url)
            if resp is None:
                return None
            css_text = self._resolve_relative_urls(resp.text, css_url)
            local_path.write_text(css_text, encoding="utf-8")
            return filename

    @staticmethod
    def _filename(css_url: str) -> str:
        """CSS URL 에서 파일명 추출 + URL 해시 접미사로 충돌 방지."""
        path = urllib.parse.urlparse(css_url).path
        name = path.rsplit("/", 1)[-1] or "style.css"
        stem, _, ext = name.rpartition(".")
        if not ext:
            stem, ext = name, "css"
        url_hash = hashlib.md5(css_url.encode()).hexdigest()[:8]
        return f"{stem}_{url_hash}.{ext}"

    @staticmethod
    def _resolve_relative_urls(css_text: str, css_url: str) -> str:
        """CSS 내 url() 상대경로를 절대 URL로 변환."""
        base = css_url.rsplit("/", 1)[0] + "/"

        def _resolve(m: re.Match) -> str:
            ref = m.group(1)
            if ref.startswith(("data:", "http://", "https://", "//")):
                return m.group(0)
            return f"url({urllib.parse.urljoin(base, ref)})"

        return CSS_URL_RE.sub(_resolve, css_text)


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


# 블로그 내부 경로 패턴: /postXXX/, /pageXXX/, /slug/ 등
BLOG_HOST_RE = re.compile(
    r"^https?://blog-ko\.lordofheroes\.com(/.*)",
    re.IGNORECASE,
)

_EXCLUDED_PATHS = frozenset(("tag", "author", "rss", "assets", "content", "public"))


class HtmlLocalizer:
    """BeautifulSoup 기반 HTML 변환 (이미지 리라이트, CSS 로컬화, JS 제거, 내부링크)."""

    def __init__(
        self,
        soup: BeautifulSoup,
        post_url: str,
        image_map: dict[str, str],
        slug_map: dict[str, str],
        category: str | None,
        css_downloader: CssDownloader,
    ) -> None:
        self._soup = soup
        self._post_url = post_url
        self._image_map = image_map
        self._slug_map = slug_map
        self._category = category
        self._css = css_downloader
        self._prefix = _img_prefix(category)
        self._assets_prefix = _assets_prefix(category)

    def localize(self) -> str:
        """모든 변환을 적용하고 HTML 문자열을 반환."""
        self._localize_css()
        self._rewrite_images()
        self._rewrite_meta_images()
        self._rewrite_style_bg_images()
        self._rewrite_internal_links()
        self._remove_scripts()
        return str(self._soup)

    # -- CSS 로컬화 --

    def _localize_css(self) -> None:
        for link in self._soup.find_all("link", rel="stylesheet"):
            href = link.get("href", "")
            if not href:
                continue
            filename = self._css.download(href)
            if filename:
                link["href"] = f"{self._assets_prefix}{filename}"

    # -- 이미지 리라이트 --

    def _rewrite_images(self) -> None:
        """<img> 태그의 src/srcset/data-src 를 로컬 경로로 리라이트."""
        for img in self._soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            new_src, mapped = _rewrite_img_src(
                src, self._post_url, self._image_map, self._prefix
            )
            img["src"] = new_src
            if img.get("data-src"):
                del img["data-src"]
            if mapped and img.get("srcset"):
                del img["srcset"]

    def _rewrite_meta_images(self) -> None:
        """og:image, twitter:image, favicon 등 메타 이미지 리라이트."""
        for meta in self._soup.find_all("meta", attrs={"content": True}):
            prop = meta.get("property", "") or meta.get("name", "")
            if "image" not in prop.lower():
                continue
            new_src, mapped = _rewrite_img_src(
                meta["content"], self._post_url, self._image_map, self._prefix
            )
            if mapped:
                meta["content"] = new_src

        # favicon
        for link in self._soup.find_all(
            "link", rel=lambda v: v and "icon" in " ".join(v).lower()
        ):
            href = link.get("href", "")
            new_src, mapped = _rewrite_img_src(
                href, self._post_url, self._image_map, self._prefix
            )
            if mapped:
                link["href"] = new_src

        # JSON-LD 내 이미지 URL — 문자열 치환으로 처리
        for script_tag in self._soup.find_all("script", type="application/ld+json"):
            if not script_tag.string:
                continue
            text = script_tag.string
            changed = False
            for match in re.finditer(r'"url"\s*:\s*"([^"]+)"', text):
                url_val = match.group(1)
                new_src, mapped = _rewrite_img_src(
                    url_val, self._post_url, self._image_map, self._prefix
                )
                if mapped:
                    text = text.replace(url_val, new_src)
                    changed = True
            if changed:
                script_tag.string = text

    def _rewrite_style_bg_images(self) -> None:
        """<style> 블록과 인라인 style 속성의 background-image url() 리라이트."""
        for style_tag in self._soup.find_all("style"):
            if not style_tag.string:
                continue
            style_tag.string = self._rewrite_bg_urls(style_tag.string)

        for tag in self._soup.find_all(style=True):
            tag["style"] = self._rewrite_bg_urls(tag["style"])

    def _rewrite_bg_urls(self, css_text: str) -> str:
        """CSS 텍스트 내 url() 참조를 image_map 기반으로 리라이트."""
        post_url = self._post_url
        image_map = self._image_map
        prefix = self._prefix

        def _replace(m: re.Match) -> str:
            url_val = m.group(2)
            if url_val.startswith("data:"):
                return m.group(0)
            abs_url = urllib.parse.urljoin(post_url, url_val)
            key = clean_url(abs_url)
            relative_path = image_map.get(key)
            if relative_path:
                return m.group(1) + prefix + relative_path + m.group(3)
            return m.group(1) + abs_url + m.group(3)

        return BG_IMAGE_RE.sub(_replace, css_text)

    # -- 내부 링크 로컬화 --

    def _rewrite_internal_links(self) -> None:
        """블로그 내부 <a href> 를 로컬 html_local 파일 상대경로로 리라이트."""
        for a in self._soup.find_all("a", href=True):
            href = a["href"]

            m = BLOG_HOST_RE.match(href)
            if m:
                path = m.group(1)
            elif href.startswith("/"):
                path = href
            else:
                continue

            segments = [s for s in path.split("/") if s]
            if not segments or segments[0] in _EXCLUDED_PATHS:
                continue

            rel_path = self._slug_map.get(segments[0])
            if rel_path is None:
                continue

            if self._category:
                a["href"] = f"../{rel_path}"
            else:
                a["href"] = rel_path

    # -- JS 제거 --

    def _remove_scripts(self) -> None:
        for script in self._soup.find_all("script"):
            script.decompose()


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
# 포스트 단위 처리
# ---------------------------------------------------------------------------


def _process_post(
    html_path: Path,
    post_url: str,
    image_map: dict[str, str],
    slug_map: dict[str, str],
    css_downloader: CssDownloader,
    html_local_dir: Path,
    failed_log: FailedLog,
) -> bool:
    try:
        html_text = html_path.read_text(encoding="utf-8")
    except OSError as e:
        failed_log.record(post_url, f"read_failed:{e}")
        return False

    soup = BeautifulSoup(html_text, "lxml")
    category = extract_category(soup)
    target_dir = html_local_dir / category if category else html_local_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    localizer = HtmlLocalizer(
        soup, post_url, image_map, slug_map, category, css_downloader,
    )
    output = localizer.localize()

    out_path = target_dir / f"{html_path.stem}.html"
    try:
        out_path.write_text(output, encoding="utf-8")
    except OSError as e:
        failed_log.record(post_url, f"write_failed:{e}")
        return False

    return True


# ---------------------------------------------------------------------------
# 실행 진입점
# ---------------------------------------------------------------------------


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
    html_index = build_html_index(html_dir, done_html_file)  # {url: Path}
    source = [(path, url) for url, path in html_index.items()]
    slug_map = _build_slug_map(source, html_dir)
    print(f"[HTML-LOCAL] slug_map 로드: {len(slug_map)}개 항목")
    if not source:
        print("[HTML-LOCAL] 소스 HTML 파일이 없습니다.")
        return

    # done 상태 로드
    done_slugs = load_done_file(done_file)
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
    css_downloader = CssDownloader(ASSETS_DIR)

    def process_fn(url: str, date: str) -> bool:
        if url in done_urls:
            return True
        html_path = url_to_path.get(url)
        if html_path is None:
            failed_log.record(url, "source_html_not_found")
            return False
        success = _process_post(
            html_path, url, image_map, slug_map,
            css_downloader, html_local_dir, failed_log,
        )
        if success:
            slug = html_path.stem
            with done_lock:
                done_slugs[slug] = url
                done_urls.add(url)
                with open(done_file, "a", encoding="utf-8") as f:
                    f.write(f"{slug}\t{url}\n")
        return success

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
