# -*- coding: utf-8 -*-
"""카테고리 목록 페이지 및 홈 인덱스 생성.

템플릿 1페이지를 fetch하여 레이아웃으로 사용하고,
포스트 카드는 로컬 HTML 메타데이터에서 생성. published_time 내림차순.
"""

import hashlib
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from asset_downloader import CssDownloader, SiteImageDownloader
from config import BLOG_BASE as _BLOG_BASE, TAG_SLUG_TO_CATEGORY
from download_html_local import HtmlLocalizer
from utils import (
    ROOT_DIR,
    build_html_index,
    ensure_utf8_console,
    extract_category,
    fetch_with_retry,
)

_LISTING_CACHE_DIR = ROOT_DIR / "listing_cache"


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _fetch_listing_template(url: str) -> BeautifulSoup | None:
    """목록 페이지 1만 fetch하여 레이아웃 템플릿으로 반환. 캐시 지원."""
    _LISTING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(url.encode()).hexdigest()[:12]
    cache_path = _LISTING_CACHE_DIR / f"{cache_key}.html"

    if cache_path.is_file():
        html = cache_path.read_text(encoding="utf-8")
    else:
        resp = fetch_with_retry(url)
        if resp is None:
            return None
        html = resp.text
        cache_path.write_text(html, encoding="utf-8")

    soup = BeautifulSoup(html, "lxml")
    if soup.find("div", class_="post-feed") is None:
        return None
    return soup


def _extract_post_meta(html_path: Path) -> dict | None:
    """로컬 HTML에서 listing 카드용 메타데이터 추출."""
    try:
        html = html_path.read_text(encoding="utf-8")
    except OSError:
        return None
    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.find("meta", property="og:title")
    title = title_tag["content"] if title_tag else html_path.stem

    desc_tag = soup.find("meta", property="og:description")
    excerpt = desc_tag["content"] if desc_tag else ""

    img_tag = soup.find("meta", property="og:image")
    image = img_tag["content"] if img_tag else ""

    time_tag = soup.find("meta", property="article:published_time")
    published_time = time_tag["content"] if time_tag else ""

    category = extract_category(soup)

    # 작성자: author-profile-image의 alt + src
    author_img_tag = soup.find("img", class_="author-profile-image")
    author_name = author_img_tag.get("alt", "") if author_img_tag else ""
    author_img = author_img_tag.get("src", "") if author_img_tag else ""

    return {
        "title": title,
        "excerpt": excerpt,
        "image": image,
        "published_time": published_time,
        "category": category,
        "author_name": author_name,
        "author_img": author_img,
        "slug": html_path.stem,
    }


def _format_date_ko(iso_str: str) -> str:
    """ISO 날짜 → '2026년 3월 9일 월요일' 형식."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        weekdays = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
        return f"{dt.year}년 {dt.month}월 {dt.day}일 {weekdays[dt.weekday()]}"
    except (ValueError, IndexError):
        return ""


def _build_post_card(
    meta: dict,
    tag_slug: str,
) -> BeautifulSoup:
    """메타데이터로 <article class="post-card"> 생성.

    href는 블로그 절대 URL로 설정 — HtmlLocalizer._rewrite_internal_links가
    로컬 상대경로로 변환.
    """
    post_url = meta["url"]
    category = meta["category"]
    tag_class = f" tag-{tag_slug}" if tag_slug else ""
    date_str = _format_date_ko(meta["published_time"])

    card_html = f"""\
<article class="post-card post{tag_class}">
<a class="post-card-image-link" href="{post_url}">
<img alt="{meta['title']}" class="post-card-image" loading="lazy"
 sizes="(max-width: 1000px) 400px, 700px" src="{meta['image']}"/>
</a>
<div class="post-card-content">
<a class="post-card-content-link" href="{post_url}">
<header class="post-card-header">
<div class="post-card-primary-tag">{category}</div>
<h2 class="post-card-title">{meta['title']}</h2>
</header>
<section class="post-card-excerpt"><p>{meta['excerpt']}</p></section>
</a>
<footer class="post-card-meta">
<ul class="author-list"><li class="author-list-item">
<div class="author-name-tooltip">{meta['author_name']}</div>
<span class="static-avatar">
<img alt="{meta['author_name']}" class="author-profile-image" src="{meta['author_img']}"/>
</span>
</li></ul>
<div class="post-card-byline-content">
<span>{meta['author_name']}</span>
<span class="post-card-byline-date"><time datetime="{meta['published_time']}">{date_str}</time></span>
</div>
</footer>
</div>
</article>"""
    return BeautifulSoup(card_html, "html.parser")


def _collect_all_post_meta(
    html_dir: Path,
    done_html_file: Path,
) -> list[dict]:
    """로컬 HTML 전체에서 메타데이터를 1회 수집. published_time 내림차순."""
    html_index = build_html_index(html_dir, done_html_file)
    results = []
    for url, html_path in html_index.items():
        meta = _extract_post_meta(html_path)
        if meta is None:
            continue
        meta["url"] = url
        meta["html_path"] = html_path
        results.append(meta)
    results.sort(key=lambda m: m["published_time"], reverse=True)
    return results


def _find_prob_linked_slugs(
    all_posts: list[dict],
) -> set[str]:
    """확률 정보 카테고리에서 2단계 링크 체인으로 도달 가능한 slug 집합 반환.

    확률 정보 포스트 → 허브 페이지 → 개별 영웅/아티팩트 페이지.
    이들은 이미 확률 정보 메뉴로 접근 가능하므로 index_all에서 제외.
    """
    no_cat_slugs = {m["slug"] for m in all_posts if not m["category"]}
    slug_to_path = {m["slug"]: m["html_path"] for m in all_posts}

    def _extract_linked(source_slugs: set[str]) -> set[str]:
        linked = set()
        for slug in source_slugs:
            path = slug_to_path.get(slug)
            if path is None or not path.is_file():
                continue
            soup = BeautifulSoup(path.read_text(encoding="utf-8"), "lxml")
            for a in soup.find_all("a", href=True):
                m = re.search(
                    r"blog-ko\.lordofheroes\.com/([^/]+)/?$", a["href"],
                )
                if m and m.group(1) in no_cat_slugs:
                    linked.add(m.group(1))
        return linked

    # 1단계: 확률 정보 → 허브
    prob_slugs = {m["slug"] for m in all_posts if m["category"] == "확률 정보"}
    level1 = _extract_linked(prob_slugs)
    # 2단계: 허브 → 개별
    level2 = _extract_linked(level1)
    return level1 | level2


def _build_listing_page(
    template_soup: BeautifulSoup,
    cards: list[BeautifulSoup],
    total_count: int,
) -> BeautifulSoup:
    """템플릿 soup의 post-feed를 생성된 카드로 교체."""
    post_feed = template_soup.find("div", class_="post-feed")
    post_feed.clear()
    for card in cards:
        post_feed.append(card)

    desc = template_soup.find("h2", class_="site-description")
    if desc:
        desc.string = f"A collection of {total_count} posts"

    for rel_val in ("next", "prev"):
        for link in template_soup.find_all("link", attrs={"rel": rel_val}):
            link.decompose()

    return template_soup


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def generate_listing_pages(
    image_map: dict[str, str],
    slug_map: dict[str, str],
    html_local_dir: Path,
    html_dir: Path,
    done_html_file: Path | None = None,
) -> None:
    """카테고리 목록 페이지와 홈 인덱스를 생성."""
    ensure_utf8_console()
    html_local_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = html_local_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    if done_html_file is None:
        done_html_file = ROOT_DIR / "done_html.csv"

    css_downloader = CssDownloader(assets_dir)
    site_img_downloader = SiteImageDownloader(assets_dir)
    generated = 0

    # 전체 메타데이터 1회 수집
    print("[LISTING] 로컬 HTML 메타데이터 수집 중...")
    all_posts = _collect_all_post_meta(html_dir, done_html_file)
    print(f"[LISTING] 전체 {len(all_posts)}개 포스트 수집 완료")

    # 카테고리별 분류
    by_category: dict[str, list[dict]] = {}
    for meta in all_posts:
        cat = meta["category"]
        by_category.setdefault(cat, []).append(meta)

    # 카테고리 목록 페이지
    for tag_slug, category in TAG_SLUG_TO_CATEGORY.items():
        tag_url = f"{_BLOG_BASE}/tag/{tag_slug}/"
        print(f"[LISTING] {category} 템플릿 로드: {tag_url}")

        template = _fetch_listing_template(tag_url)
        if template is None:
            print(f"[LISTING] {category} 템플릿 실패, 건너뜀")
            continue

        posts = by_category.get(category, [])
        print(f"[LISTING] {category}: {len(posts)}개 포스트 (로컬)")

        cards = [_build_post_card(m, tag_slug) for m in posts]
        combined_soup = _build_listing_page(template, cards, len(posts))

        localizer = HtmlLocalizer(
            combined_soup, tag_url, image_map, slug_map,
            category, css_downloader, site_img_downloader,
        )
        output = localizer.localize()

        target_dir = html_local_dir / category
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "index.html").write_text(output, encoding="utf-8")
        generated += 1

    # 블로그 홈페이지 전체 목록 → html_local/index_all.html
    # 확률 정보 링크 체인으로 도달 가능한 페이지 제외 (이미 메뉴로 접근 가능)
    prob_slugs = _find_prob_linked_slugs(all_posts)
    index_all_posts = [m for m in all_posts if m["slug"] not in prob_slugs]
    print(
        f"[LISTING] 확률 정보 링크 체인 {len(prob_slugs)}건 제외 "
        f"→ index_all 대상: {len(index_all_posts)}건"
    )

    home_url = f"{_BLOG_BASE}/"
    print(f"[LISTING] 홈페이지 템플릿 로드: {home_url}")

    template = _fetch_listing_template(home_url)
    if template is None:
        print("[LISTING] 홈페이지 템플릿 실패")
    else:
        cards = [_build_post_card(m, "") for m in index_all_posts]
        combined_soup = _build_listing_page(template, cards, len(index_all_posts))

        localizer = HtmlLocalizer(
            combined_soup, home_url, image_map, slug_map,
            "", css_downloader, site_img_downloader,
        )
        output = localizer.localize()

        (html_local_dir / "index_all.html").write_text(output, encoding="utf-8")
        generated += 1

    print(f"[LISTING 완료] {generated}개 목록 페이지 생성")
