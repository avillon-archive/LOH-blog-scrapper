# -*- coding: utf-8 -*-
"""오프라인 열람용 HTML 생성.

이미 다운로드된 html/ 파일을 기반으로:
  1. 이미지 경로를 image_map.csv 기반 로컬 상대경로로 리라이트
  2. 모든 <script> 태그 제거
  3. CSS를 로컬에 저장하고 href를 상대경로로 교체
  4. html_local/ 에 저장
"""

import re
import threading
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup

from asset_downloader import CssDownloader, SiteImageDownloader
from config import BLOG_BASE as _BLOG_BASE, BLOG_HOST_RE, TAG_SLUG_TO_CATEGORY
from log_io import append_line, csv_line
from utils import (
    DEFAULT_MAX_WORKERS,
    ROOT_DIR,
    FailedLog,
    LineBuffer,
    build_html_index,
    clean_url,
    ensure_utf8_console,
    extract_category,
    load_done_file,
    load_image_map,
    load_stale,
    run_pipeline,
)

HTML_DIR = ROOT_DIR / "html"
HTML_LOCAL_DIR = ROOT_DIR / "html_local"
ASSETS_DIR = HTML_LOCAL_DIR / "assets"
IMAGE_MAP_FILE = ROOT_DIR / "image_map.csv"
DONE_FILE = ROOT_DIR / "done_html_local.csv"
FAILED_FILE = ROOT_DIR / "failed_html_local.csv"
STALE_FILE = ROOT_DIR / "stale_html_local.csv"

# 인라인 style background-image 패턴
BG_IMAGE_RE = re.compile(
    r"""(background(?:-image)?\s*:[^;]*url\(\s*['"]?)([^'")]+)(['"]?\s*\))""",
    re.IGNORECASE,
)

_EXCLUDED_PATHS = frozenset(("tag", "author", "rss", "assets", "content", "public"))


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
    site_img: SiteImageDownloader | None = None,
    assets_prefix: str = "",
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
    # 블로그 사이트 크롬 이미지 → assets/ 다운로드
    if site_img:
        filename = site_img.download(abs_src)
        if filename:
            return f"{assets_prefix}{filename}", True
    return abs_src, False


# ---------------------------------------------------------------------------
# HTML 로컬화
# ---------------------------------------------------------------------------


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
        site_image_downloader: SiteImageDownloader | None = None,
    ) -> None:
        self._soup = soup
        self._post_url = post_url
        self._image_map = image_map
        self._slug_map = slug_map
        self._category = category
        self._css = css_downloader
        self._site_img = site_image_downloader
        self._prefix = _img_prefix(category)
        self._assets_prefix = _assets_prefix(category)
        self.unmapped_urls: set[str] = set()

    def localize(self) -> str:
        """모든 변환을 적용하고 HTML 문자열을 반환."""
        self._localize_css()
        self._rewrite_images()
        self._rewrite_meta_images()
        self._rewrite_style_bg_images()
        self._rewrite_anchor_assets()
        self._rewrite_internal_links()
        self._rewrite_home_logo()
        self._fix_youtube_iframes()
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

    def _rewrite_img(self, src: str) -> tuple[str, bool]:
        """_rewrite_img_src 호출을 간소화하는 내부 헬퍼."""
        new_src, mapped = _rewrite_img_src(
            src, self._post_url, self._image_map, self._prefix,
            self._site_img, self._assets_prefix,
        )
        if not mapped and src and not src.startswith("data:"):
            abs_src = urllib.parse.urljoin(self._post_url, src)
            if abs_src.startswith("http"):
                self.unmapped_urls.add(clean_url(abs_src))
        return new_src, mapped

    def _rewrite_images(self) -> None:
        """<img> 태그의 src/srcset/data-src 를 로컬 경로로 리라이트."""
        for img in self._soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            new_src, mapped = self._rewrite_img(src)
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
            new_src, mapped = self._rewrite_img(meta["content"])
            if mapped:
                meta["content"] = new_src

        # favicon
        for link in self._soup.find_all("link", rel="icon"):
            href = link.get("href", "")
            new_src, mapped = self._rewrite_img(href)
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
                new_src, mapped = self._rewrite_img(url_val)
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

        def _replace(m: re.Match) -> str:
            url_val = m.group(2)
            if url_val.startswith("data:"):
                return m.group(0)
            new_src, _ = self._rewrite_img(url_val)
            return m.group(1) + new_src + m.group(3)

        return BG_IMAGE_RE.sub(_replace, css_text)

    # -- 앵커 링크 로컬화 --

    def _rewrite_anchor_assets(self) -> None:
        """<a href>가 image_map에 있는 파일을 가리키면 로컬 경로로 치환."""
        for a in self._soup.find_all("a", href=True):
            href = a["href"]
            if not href or not href.startswith("http"):
                continue
            abs_href = urllib.parse.urljoin(self._post_url, href)
            key = clean_url(abs_href)
            relative_path = self._image_map.get(key)
            if relative_path:
                a["href"] = f"{self._prefix}{relative_path}"

    def _rewrite_internal_links(self) -> None:
        """블로그 내부 <a href> 를 로컬 html_local 파일 상대경로로 리라이트."""
        for a in self._soup.find_all("a", href=True):
            href = a["href"]

            m = BLOG_HOST_RE.match(href)
            if m:
                path = m.group(1) or "/"
            elif href.startswith("/"):
                path = href
            else:
                continue

            segments = [s for s in path.split("/") if s]
            if not segments:
                # 루트 경로 → 홈 index
                if self._category:
                    a["href"] = "../index.html"
                else:
                    a["href"] = "index.html"
                continue

            # /tag/{slug}/ → 카테고리 목록 페이지
            if segments[0] == "tag" and len(segments) >= 2:
                category = TAG_SLUG_TO_CATEGORY.get(segments[1])
                if category:
                    if self._category:
                        a["href"] = f"../{category}/index.html"
                    else:
                        a["href"] = f"{category}/index.html"
                    continue

            if segments[0] in _EXCLUDED_PATHS:
                continue

            rel_path = self._slug_map.get(segments[0])
            if rel_path is None:
                continue

            if self._category:
                a["href"] = f"../{rel_path}"
            else:
                a["href"] = rel_path

    def _rewrite_home_logo(self) -> None:
        """블로그 홈 로고 링크를 로컬 index.html로 리라이트."""
        target = "../index.html" if self._category else "index.html"
        for logo in self._soup.find_all("a", class_="site-nav-logo"):
            if logo.get("href"):
                logo["href"] = target

    # -- YouTube / JS --

    def _fix_youtube_iframes(self) -> None:
        """YouTube iframe의 width/height 제거 → CSS 반응형 처리."""
        for iframe in self._soup.find_all("iframe", src=True):
            src = iframe["src"]
            if "youtube.com" not in src:
                continue
            if iframe.has_attr("width"):
                del iframe["width"]
            if iframe.has_attr("height"):
                del iframe["height"]
            iframe["style"] = "width:100%; aspect-ratio:16/9;"

    def _remove_scripts(self) -> None:
        for script in self._soup.find_all("script"):
            script.decompose()


# ---------------------------------------------------------------------------
# 슬러그 맵
# ---------------------------------------------------------------------------


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
    site_image_downloader: SiteImageDownloader | None = None,
    stale_buf: LineBuffer | None = None,
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
        site_image_downloader,
    )
    output = localizer.localize()

    if stale_buf and localizer.unmapped_urls:
        stale_buf.add(csv_line(post_url, "|".join(localizer.unmapped_urls)))

    # 블로그 홈페이지는 index.html 로 저장
    if post_url.rstrip("/") == _BLOG_BASE:
        out_name = "index.html"
    else:
        out_name = f"{html_path.stem}.html"
    out_path = target_dir / out_name
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
    done_html_file: Path = ROOT_DIR / "done_html.csv",
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
            print(f"[HTML-LOCAL] stale {len(stale)}개 중 {len(refresh_urls)}개 포스트 재생성")
        else:
            print(f"[HTML-LOCAL] stale {len(stale)}개 항목, refresh 대상 없음")

    # ── stale 파일 재작성 ──────────────────────────────────────────────
    STALE_FILE.unlink(missing_ok=True)
    stale_buf = LineBuffer(STALE_FILE, flush_every=50, header="post_url,unmapped_urls")
    for url, unmapped in stale.items():
        if url not in refresh_urls:
            stale_buf.add(csv_line(url, "|".join(unmapped)))

    # run_pipeline 용 (url, date) 형식으로 변환
    url_to_path: dict[str, Path] = {url: path for path, url in source}
    posts = [(url, "") for _, url in source]

    if not retry_mode and not refresh_urls:
        pending = sum(1 for url, _ in posts if url not in done_urls)
        if pending == 0:
            print(f"[HTML-LOCAL] {len(posts)}개 포스트 모두 처리 완료, 건너뜀")
            stale_buf.flush_all()
            return

    done_lock = threading.Lock()
    fail_lock = threading.Lock()
    failed_log = FailedLog(failed_file, fail_lock)
    css_downloader = CssDownloader(ASSETS_DIR)
    site_img_downloader = SiteImageDownloader(ASSETS_DIR)

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
            site_img_downloader,
            stale_buf=stale_buf,
        )
        if success:
            slug = html_path.stem
            with done_lock:
                done_slugs[slug] = url
                done_urls.add(url)
            append_line(done_file, csv_line(slug, url), header="slug,post_url")
        return success

    run_pipeline(
        posts,
        process_fn,
        failed_log,
        retry_mode,
        label="HTML-LOCAL",
        max_workers=max_workers,
    )
    stale_buf.flush_all()

    # 카테고리 목록 페이지 생성 (지연 임포트: 순환 방지)
    from listing_pages import generate_listing_pages
    generate_listing_pages(image_map, slug_map, html_local_dir, html_dir)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="오프라인 열람용 HTML 생성")
    parser.add_argument("--retry", action="store_true", help="실패 목록 재처리")
    parser.add_argument("--force", action="store_true", help="기존 기록 무시하고 전체 재생성")
    args = parser.parse_args()

    run_html_local(retry_mode=args.retry, force_download=args.force)
