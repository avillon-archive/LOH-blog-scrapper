"""
build_posts_list.py - parse sitemap-posts.xml / sitemap-pages.xml and build
                      all_posts.txt / all_pages.txt / all_links.txt
"""
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from bs4 import BeautifulSoup

from config import MULTILANG_CONFIGS, ROOT_DIR, SITEMAP_PAGES_URL, SITEMAP_URL
from utils import build_html_index, fetch_with_retry, load_posts

OUTPUT_FILE = ROOT_DIR / "all_posts.txt"
PAGES_OUTPUT_FILE = ROOT_DIR / "all_pages.txt"
LINKS_OUTPUT_FILE = ROOT_DIR / "all_links.txt"
HTML_DIR = ROOT_DIR / "html"
DONE_HTML_FILE = ROOT_DIR / "done_html.txt"

# Date extractor from <lastmod> values.
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def fetch_sitemap(url: str) -> str:
    resp = fetch_with_retry(url, timeout=30)
    if resp is None:
        raise ConnectionError(f"sitemap fetch failed (3회 재시도 후 응답 없음): {url}")
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_child_text(node: ET.Element, wanted_local_name: str) -> str:
    for child in node:
        if _local_name(child.tag) == wanted_local_name:
            return (child.text or "").strip()
    return ""


def parse_sitemap(xml: str) -> list[tuple[str, str]]:
    """
    Parse sitemap and return [(loc, lastmod_date), ...].
    If lastmod is missing or unparseable, date is "".
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ValueError(f"XML parse failed: {e}") from e

    entries: list[tuple[str, str]] = []
    for node in root.iter():
        if _local_name(node.tag) != "url":
            continue
        loc = _find_child_text(node, "loc")
        if not loc:
            continue

        lastmod_raw = _find_child_text(node, "lastmod")
        date = ""
        if lastmod_raw:
            date_m = DATE_RE.search(lastmod_raw)
            if date_m:
                date = date_m.group(1)

        entries.append((loc, date))

    return entries


def _build_sitemap_file(
    sitemap_url: str, output_file: Path
) -> tuple[int, list[tuple[str, str]]]:
    """범용 단일 사이트맵 fetch → 정렬 → 파일 저장.

    기존 파일에 published_time(3번째 컬럼)이 있으면 보존한다.

    Returns:
        (작성된 URL 항목 수, entries 리스트).
    Raises:
        Exception: sitemap fetch/parse 실패 또는 항목 없음.
    """
    ROOT_DIR.mkdir(parents=True, exist_ok=True)

    existing_published: dict[str, str] = {}
    if output_file.exists():
        for url, _lastmod, published in load_posts(output_file):
            if published:
                existing_published[url] = published

    xml = fetch_sitemap(sitemap_url)
    entries = parse_sitemap(xml)
    if not entries:
        raise ValueError(
            f"no URL entries parsed from {sitemap_url}. "
            "please inspect sitemap XML structure."
        )

    # 날짜 내림차순 정렬 (날짜 없는 항목은 맨 뒤)
    entries.sort(key=lambda x: (x[1] != "", x[1]), reverse=True)

    lines: list[str] = []
    for url, date in entries:
        pub = existing_published.get(url, "")
        lines.append(f"{url}\t{date}\t{pub}" if pub else f"{url}\t{date}")
    output_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(entries), entries


def fetch_newest_single_sitemap_date(sitemap_url: str) -> str:
    """단일 사이트맵 URL에서 가장 최신 lastmod 날짜(YYYY-MM-DD)를 반환한다.

    실패 시 "" 반환.
    """
    try:
        xml = fetch_sitemap(sitemap_url)
        entries = parse_sitemap(xml)
        dated = [d for _, d in entries if d]
        return max(dated) if dated else ""
    except Exception:
        return ""


def fetch_newest_sitemap_date() -> str:
    """posts + pages 사이트맵에서 가장 최신 lastmod 날짜(YYYY-MM-DD)를 반환한다.

    두 사이트맵 중 하나라도 최신 날짜를 얻으면 그 중 최대값을 반환한다.
    둘 다 실패 시 "" 반환.
    """
    dates: list[str] = []
    for url in (SITEMAP_URL, SITEMAP_PAGES_URL):
        d = fetch_newest_single_sitemap_date(url)
        if d:
            dates.append(d)
    return max(dates) if dates else ""


def build_and_write() -> tuple[int, list[tuple[str, str]]]:
    """sitemap-posts.xml → all_posts.txt 갱신.

    Returns:
        (작성된 URL 항목 수, entries 리스트).
    Raises:
        Exception: sitemap fetch/parse 실패 또는 항목 없음.
    """
    return _build_sitemap_file(SITEMAP_URL, OUTPUT_FILE)


def build_pages_and_write() -> tuple[int, list[tuple[str, str]]]:
    """sitemap-pages.xml → all_pages.txt 갱신.

    Returns:
        (작성된 URL 항목 수, entries 리스트).
    Raises:
        Exception: sitemap fetch/parse 실패 또는 항목 없음.
    """
    return _build_sitemap_file(SITEMAP_PAGES_URL, PAGES_OUTPUT_FILE)


def build_multilang_and_write() -> dict[str, tuple[int, list[tuple[str, str]]]]:
    """EN/JA sitemap-posts/pages → all_posts/all_pages/all_links 갱신.

    Returns:
        {lang: (posts 항목 수, entries)} dict.  실패한 언어는 포함되지 않는다.
    """
    results: dict[str, tuple[int, list[tuple[str, str]]]] = {}
    for lang, cfg in MULTILANG_CONFIGS.items():
        # posts
        try:
            count, entries = _build_sitemap_file(cfg["sitemap_posts"], cfg["all_posts"])
            results[lang] = (count, entries)
            print(f"[포스트 목록] {cfg['all_posts'].name} 갱신 완료 ({count}개 URL)")
        except Exception as e:
            print(f"[포스트 목록] {cfg['all_posts'].name} 갱신 실패: {e}")

        # pages
        try:
            count_pages, _ = _build_sitemap_file(cfg["sitemap_pages"], cfg["all_pages"])
            print(f"[포스트 목록] {cfg['all_pages'].name} 갱신 완료 ({count_pages}개 URL)")
        except Exception as e:
            print(f"[포스트 목록] {cfg['all_pages'].name} 갱신 실패: {e}")

        # links (posts + pages 병합)
        try:
            count_links = _build_multilang_links(cfg)
            print(f"[포스트 목록] {cfg['all_links'].name} 갱신 완료 ({count_links}개 URL)")
        except Exception as e:
            print(f"[포스트 목록] {cfg['all_links'].name} 갱신 실패: {e}")

    return results


def _build_multilang_links(cfg: dict[str, str | Path]) -> int:
    """posts + pages → all_links 병합. build_links_and_write() 와 동일 로직."""
    merged: dict[str, tuple[str, str]] = {}

    # pages 먼저 로드 후 posts로 덮어써서 posts 우선
    for filepath in (cfg["all_pages"], cfg["all_posts"]):
        for url, date, published in load_posts(filepath):
            if url:
                merged[url] = (date, published)

    if not merged:
        raise ValueError(f"병합 대상 없음: {cfg['all_posts'].name} / {cfg['all_pages'].name}")

    entries = sorted(
        merged.items(),
        key=lambda x: (x[1][0] != "", x[1][0]),
        reverse=True,
    )

    lines: list[str] = []
    for url, (date, published) in entries:
        lines.append(f"{url}\t{date}\t{published}" if published else f"{url}\t{date}")
    cfg["all_links"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(entries)


def build_links_and_write() -> int:
    """all_posts.txt + all_pages.txt → all_links.txt 병합·중복 제거.

    posts 파일 항목이 우선. 동일 URL이 양쪽에 있으면 posts 날짜를 사용.
    날짜 내림차순 정렬 (날짜 없는 항목은 맨 뒤).

    Returns:
        저장된 항목 수.
    """
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    merged: dict[str, tuple[str, str]] = {}

    # pages 먼저 로드 후 posts로 덮어써서 posts 우선 보장
    for filepath in (PAGES_OUTPUT_FILE, OUTPUT_FILE):
        for url, date, published in load_posts(filepath):
            if url:
                merged[url] = (date, published)

    if not merged:
        raise ValueError(
            "all_links.txt 병합 대상이 없습니다. "
            f"{OUTPUT_FILE.name} / {PAGES_OUTPUT_FILE.name} 를 확인하세요."
        )

    entries = sorted(
        merged.items(),
        key=lambda x: (x[1][0] != "", x[1][0]),
        reverse=True,
    )

    lines: list[str] = []
    for url, (date, published) in entries:
        lines.append(f"{url}\t{date}\t{published}" if published else f"{url}\t{date}")
    LINKS_OUTPUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(entries)


def fill_published_times(
    posts_file: Path | None = None,
    html_dir: Path | None = None,
    done_html_file: Path | None = None,
) -> int:
    """로컬 HTML에서 article:published_time을 추출하여 포스트 파일에 채운다.

    Returns:
        새로 채운 항목 수.
    """
    posts_file = posts_file or OUTPUT_FILE
    html_dir = html_dir or HTML_DIR
    done_html_file = done_html_file or DONE_HTML_FILE
    posts = load_posts(posts_file)
    if not posts:
        return 0

    html_index = build_html_index(html_dir, done_html_file)
    filled = 0
    updated: list[tuple[str, str, str]] = []

    for url, lastmod, published in posts:
        if published:
            updated.append((url, lastmod, published))
            continue

        html_path = html_index.get(url)
        if not html_path or not html_path.exists():
            updated.append((url, lastmod, ""))
            continue

        html_text = html_path.read_text(encoding="utf-8")
        soup = BeautifulSoup(html_text, "lxml")
        meta = soup.find("meta", property="article:published_time")
        pub_value = meta["content"].strip() if meta and meta.get("content") else ""
        if pub_value:
            filled += 1
        updated.append((url, lastmod, pub_value))

    lines: list[str] = []
    for url, lastmod, published in updated:
        lines.append(f"{url}\t{lastmod}\t{published}" if published else f"{url}\t{lastmod}")
    posts_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[published_time] {filled}개 항목 채움 (총 {len(updated)}개)")
    return filled


def _print_sitemap_summary(
    label: str, count: int, output_file: Path, entries: list[tuple[str, str]]
) -> None:
    print(f"[done] wrote {count} URLs to {output_file}  [{label}]")
    no_date = sum(1 for _, d in entries if not d)
    if no_date:
        print(f"  - entries without lastmod date: {no_date}")
    dated = [(u, d) for u, d in entries if d]
    if dated:
        print(f"  newest: {dated[0][1]}  {dated[0][0]}")
        print(f"  oldest: {dated[-1][1]}  {dated[-1][0]}")


def main():
    print(f"[sitemap download] posts : {SITEMAP_URL}")
    print(f"[sitemap download] pages : {SITEMAP_PAGES_URL}")
    print()

    # ── posts ──────────────────────────────────────────────────────────
    try:
        count_posts, entries_posts = build_and_write()
        _print_sitemap_summary("posts", count_posts, OUTPUT_FILE, entries_posts)
    except Exception as e:
        print(f"[error] posts sitemap: {e}")
        sys.exit(1)
    print()

    # ── pages ──────────────────────────────────────────────────────────
    try:
        count_pages, entries_pages = build_pages_and_write()
        _print_sitemap_summary("pages", count_pages, PAGES_OUTPUT_FILE, entries_pages)
    except Exception as e:
        print(f"[error] pages sitemap: {e}")
        sys.exit(1)
    print()

    # ── links (merge) ──────────────────────────────────────────────────
    try:
        count_links = build_links_and_write()
        print(f"[done] wrote {count_links} URLs to {LINKS_OUTPUT_FILE}  [links]")
    except Exception as e:
        print(f"[error] links merge: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
