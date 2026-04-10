# -*- coding: utf-8 -*-
"""공지사항 heading 기반 이미지 폴백.

동일 섹션이 날짜별로 반복 등장하는 공지사항 포스트에서,
깨진 이미지를 다른 날짜 포스트의 같은 섹션에서 위치 기반으로 복구한다.
"""

import difflib
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from .fetch import _fetch_image, _fetch_wayback_image, _get_content_tag
from .url_utils import _clean_img_url


# ── 상수 ────────────────────────────────────────────────────────────────────

_HEADING_SIMILARITY_THRESHOLD = 0.90
_DATE_RANGE_DAYS = 62  # ±2개월 근사
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


# ── 유사도 ──────────────────────────────────────────────────────────────────


def _heading_similarity(a: str, b: str) -> float:
    """두 heading 텍스트의 유사도를 반환한다 (0.0~1.0)."""
    return difflib.SequenceMatcher(None, a, b).ratio()


# ── 섹션 이미지 수집 (h* 태그 기준) ─────────────────────────────────────────


def _collect_section_images(
    content_tag, heading_tag: Tag, post_url: str,
) -> list[str]:
    """heading_tag(h1-h6)부터 다음 h*(또는 content 끝)까지의 <img> URL을 수집한다."""
    imgs: list[str] = []
    for sib in heading_tag.next_siblings:
        if hasattr(sib, "name") and sib.name in _HEADING_TAGS:
            break
        if not hasattr(sib, "find_all"):
            continue
        for img in sib.find_all("img"):
            if "author-profile-image" in (img.get("class") or []):
                continue
            if img.find_parent("div", class_="author-card"):
                continue
            src = img.get("src") or img.get("data-src") or ""
            if src:
                abs_url = urllib.parse.urljoin(post_url, src)
                imgs.append(_clean_img_url(abs_url))
    return imgs


# ── 섹션 heading 텍스트 수집 (h* + strong) ──────────────────────────────────


def _collect_section_heading_texts(content_tag) -> list[str]:
    """h1-h6 텍스트 + 블록 레벨 strong 텍스트를 모두 수집한다.

    도너 검색 시 매칭 대상으로 사용. 섹션 경계는 h* 태그만 사용.
    """
    texts: list[str] = []
    seen: set[str] = set()
    for tag in content_tag.find_all(list(_HEADING_TAGS) + ["strong"]):
        text = tag.get_text(strip=True)
        if not text or text in seen:
            continue
        if tag.name in _HEADING_TAGS:
            seen.add(text)
            texts.append(text)
        elif tag.name == "strong":
            parent = tag.parent
            if parent and parent.name not in _HEADING_TAGS:
                seen.add(text)
                texts.append(text)
    return texts


# ── heading context 탐색 ───────────────────────────────────────────────────


def _find_heading_context(
    soup: BeautifulSoup, post_url: str, img_url: str,
) -> tuple[str, int, int] | None:
    """깨진 이미지가 속한 h* 섹션의 (heading_text, position, section_total)을 반환한다.

    position은 0-based. 해당 없으면 None.
    """
    clean_target = _clean_img_url(img_url)
    content_tag = _get_content_tag(soup)

    for heading in content_tag.find_all(_HEADING_TAGS):
        heading_text = heading.get_text(strip=True)
        if not heading_text:
            continue
        section_imgs = _collect_section_images(content_tag, heading, post_url)
        for pos, section_img_url in enumerate(section_imgs):
            if section_img_url == clean_target:
                return (heading_text, pos, len(section_imgs))
    return None


# ── 날짜 유틸 ──────────────────────────────────────────────────────────────


def _parse_date(date_str: str) -> date | None:
    """YYYY-MM-DD 문자열을 date 객체로 변환한다."""
    if not date_str or len(date_str) < 10:
        return None
    try:
        parts = date_str[:10].split("-")
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return None


# ── 도너 포스트 검색 ───────────────────────────────────────────────────────


def _find_donor_posts(
    heading_text: str,
    post_url: str,
    post_date: str,
    posts: list[tuple[str, str, str]],
    html_index: dict[str, Path],
) -> list[tuple[str, str, Path]]:
    """heading_text와 유사한 heading이 있는 ±2개월 공지사항 포스트를 날짜 근접순으로 반환."""
    origin_date = _parse_date(post_date)
    if origin_date is None:
        return []

    date_lo = origin_date - timedelta(days=_DATE_RANGE_DAYS)
    date_hi = origin_date + timedelta(days=_DATE_RANGE_DAYS)

    # 1단계: ±2개월 범위 + 공지사항 카테고리 필터 (HTML 파싱 없이 path로 판별)
    candidates: list[tuple[str, str, Path, int]] = []
    for url, lastmod, pub_time, *_ in posts:
        if url == post_url:
            continue
        path = html_index.get(url)
        if path is None or not path.exists():
            continue
        path_str = path.as_posix()
        if "/공지사항/" not in path_str:
            continue
        check_date_str = pub_time[:10] if pub_time else lastmod
        cand_date = _parse_date(check_date_str)
        if cand_date is None or cand_date < date_lo or cand_date > date_hi:
            continue
        abs_days = abs((cand_date - origin_date).days)
        candidates.append((url, check_date_str, path, abs_days))

    candidates.sort(key=lambda x: x[3])

    # 2단계: HTML 파싱하여 heading 유사도 확인 (h* + strong 모두 탐색)
    donors: list[tuple[str, str, Path]] = []
    for url, cand_date, path, _ in candidates:
        try:
            html_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        soup = BeautifulSoup(html_text, "lxml")
        content_tag = _get_content_tag(soup)
        for text in _collect_section_heading_texts(content_tag):
            if _heading_similarity(heading_text, text) >= _HEADING_SIMILARITY_THRESHOLD:
                donors.append((url, cand_date, path))
                break
    return donors


# ── 메인 진입점 ────────────────────────────────────────────────────────────


def _fetch_heading_fallback(
    img_url: str,
    post_url: str,
    post_date: str,
    soup: BeautifulSoup,
    posts: list[tuple[str, str, str]],
    html_index: dict[str, Path],
    image_map: dict[str, str],
) -> tuple[bytes, str, str, str] | None:
    """heading 기반 폴백으로 이미지를 다운로드한다.

    Returns:
        성공 시 (content, final_url, content_type, content_disposition) 4-tuple.
        실패 시 None.
    """
    ctx = _find_heading_context(soup, post_url, img_url)
    if ctx is None:
        return None
    heading_text, position, section_total = ctx
    if not heading_text:
        return None

    donors = _find_donor_posts(heading_text, post_url, post_date, posts, html_index)
    if not donors:
        return None

    for donor_url, donor_date, donor_path in donors:
        try:
            donor_html = donor_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        donor_soup = BeautifulSoup(donor_html, "lxml")
        donor_content = _get_content_tag(donor_soup)

        # 도너에서 매칭 섹션 찾기 — h* 태그 섹션 우선, 없으면 strong 섹션
        matched_imgs: list[str] | None = None

        # h* 태그 섹션 탐색
        for h_tag in donor_content.find_all(_HEADING_TAGS):
            donor_heading = h_tag.get_text(strip=True)
            if not donor_heading:
                continue
            if _heading_similarity(heading_text, donor_heading) < _HEADING_SIMILARITY_THRESHOLD:
                continue
            matched_imgs = _collect_section_images(donor_content, h_tag, donor_url)
            break

        # h* 매칭 실패 시 strong 섹션에서 이미지 수집
        # strong은 블록 레벨 구분자 역할이지만 DOM 구조가 다르므로
        # strong 부모의 next_siblings에서 다음 strong/h*까지 이미지 수집
        if matched_imgs is None:
            for strong in donor_content.find_all("strong"):
                if strong.parent and strong.parent.name in _HEADING_TAGS:
                    continue
                donor_heading = strong.get_text(strip=True)
                if not donor_heading:
                    continue
                if _heading_similarity(heading_text, donor_heading) < _HEADING_SIMILARITY_THRESHOLD:
                    continue
                # strong의 부모(p 등)의 next_siblings에서 이미지 수집
                start = strong.parent or strong
                imgs: list[str] = []
                for sib in start.next_siblings:
                    if not hasattr(sib, "name") or sib.name is None:
                        continue
                    if sib.name in _HEADING_TAGS:
                        break
                    # strong 구분자 체크
                    for inner_strong in (sib.find_all("strong") if hasattr(sib, "find_all") else []):
                        if inner_strong.parent and inner_strong.parent.name not in _HEADING_TAGS:
                            inner_text = inner_strong.get_text(strip=True)
                            if inner_text and inner_text != donor_heading:
                                break
                    else:
                        if hasattr(sib, "find_all"):
                            for img in sib.find_all("img"):
                                if "author-profile-image" in (img.get("class") or []):
                                    continue
                                if img.find_parent("div", class_="author-card"):
                                    continue
                                src = img.get("src") or img.get("data-src") or ""
                                if src:
                                    imgs.append(_clean_img_url(
                                        urllib.parse.urljoin(donor_url, src)))
                        continue
                    break
                matched_imgs = imgs
                break

        if matched_imgs is None:
            continue

        # 이미지 개수 가드
        if len(matched_imgs) != section_total:
            continue

        if position >= len(matched_imgs):
            continue

        donor_img_url = matched_imgs[position]

        # 다운로드 시도
        payload = _fetch_image(donor_img_url)
        if payload is None:
            payload = _fetch_wayback_image(donor_img_url)
        if payload is not None:
            print(f"  [heading] {img_url[:60]} → {donor_url} ({donor_date})")
            return payload
        # 이 도너에서 실패, 다음 도너 시도

    return None
