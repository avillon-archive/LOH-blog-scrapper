"""
build_posts_list.py - parse sitemap-posts.xml and build all_posts.txt
"""
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from utils import fetch_with_retry

SITEMAP_URL = "https://blog-ko.lordofheroes.com/sitemap-posts.xml"
ROOT_DIR = Path(__file__).parent / "loh_blog"
OUTPUT_FILE = ROOT_DIR / "all_posts.txt"

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


def fetch_newest_sitemap_date() -> str:
    """사이트맵을 파싱해 가장 최신 lastmod 날짜(YYYY-MM-DD)를 반환한다.

    실패 시 "" 반환.
    """
    try:
        xml = fetch_sitemap(SITEMAP_URL)
        entries = parse_sitemap(xml)
    except Exception:
        return ""
    dated = [d for _, d in entries if d]
    return max(dated) if dated else ""


def build_and_write() -> tuple[int, list[tuple[str, str]]]:
    """사이트맵을 파싱해 all_posts.txt를 갱신한다.

    Returns:
        (작성된 URL 항목 수, entries 리스트).
    Raises:
        Exception: sitemap fetch/parse 실패 또는 항목 없음.
    """
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
    xml = fetch_sitemap(SITEMAP_URL)
    entries = parse_sitemap(xml)
    if not entries:
        raise ValueError("no URL entries parsed. please inspect sitemap XML structure.")

    # 날짜 내림차순 정렬 (날짜 없는 항목은 맨 뒤)
    # key: (날짜 있음=True > 없음=False, 날짜 문자열) → reverse=True 로 최신순
    entries.sort(key=lambda x: (x[1] != "", x[1]), reverse=True)

    OUTPUT_FILE.write_text(
        "\n".join(f"{url}\t{date}" for url, date in entries) + "\n",
        encoding="utf-8",
    )
    return len(entries), entries


def main():
    print(f"[sitemap download] {SITEMAP_URL}")
    try:
        count, entries = build_and_write()
    except Exception as e:
        print(f"[error] {e}")
        sys.exit(1)

    # 결과 요약 출력
    no_date = sum(1 for _, d in entries if not d)
    print(f"[done] wrote {count} URLs to {OUTPUT_FILE}")
    if no_date:
        print(f"  - entries without lastmod date: {no_date}")

    dated = [(u, d) for u, d in entries if d]
    if dated:
        print(f"  newest: {dated[0][1]}  {dated[0][0]}")
        print(f"  oldest: {dated[-1][1]}  {dated[-1][0]}")


if __name__ == "__main__":
    main()
