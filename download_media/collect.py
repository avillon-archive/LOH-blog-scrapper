# -*- coding: utf-8 -*-
"""현재 blog HTML 에서 미디어 URL 수집 (Cat A/B/D).

반환 구조: list[MediaItem] = list[tuple[url, mtype, anchor_type, anchor_text]]

- Cat A: GDrive 앵커 중 BGM/OST 컨텍스트로 --images 가 제외한 것 (→ gdrive_audio)
- Cat B: <video>/<audio>/<source> 태그 + <video poster=> (→ video_tag/audio_tag/video_poster)
- Cat D: 직접 .mp4/.mp3 등 확장자를 가진 앵커 (→ anchor_direct)

YouTube/Vimeo iframe 은 수집하지 않는다.
"""

import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup

from config import MEDIA_REMOTE_REWRITES
from download_images.collect import _detect_non_image_urls
from download_images.fetch import _get_content_tag
from download_images.url_utils import _clean_img_url

from .constants import MEDIA_EXTS

# 수집 제외 호스트 (임베드 전용)
_EMBED_HOSTS = {
    "youtube.com", "www.youtube.com", "youtu.be",
    "vimeo.com", "www.vimeo.com", "player.vimeo.com",
}


def _is_embed_host(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in _EMBED_HOSTS


def _abs_url(post_url: str, src: str) -> str:
    return urllib.parse.urljoin(post_url, (src or "").strip())


def collect_media_urls(
    soup: BeautifulSoup,
    post_url: str,
) -> list[tuple[str, str, str, str]]:
    """현재 blog HTML 에서 인라인 미디어 항목을 추출한다."""
    results: list[tuple[str, str, str, str]] = []
    seen_keys: set[tuple[str, str]] = set()

    def _add(url: str, mtype: str) -> None:
        if not url or not url.startswith("http"):
            return
        if _is_embed_host(url):
            return
        # 원격 리라이트 대상은 수집 스킵 (R2 등에서 직접 서빙)
        if _clean_img_url(url) in MEDIA_REMOTE_REWRITES:
            return
        key = (mtype, _clean_img_url(url))
        if key in seen_keys:
            return
        seen_keys.add(key)
        results.append((url, mtype, "inline", ""))

    content_tag = _get_content_tag(soup)

    # ── Cat B: <video>/<audio>/<source> ────────────────────────────────
    for tag_name, mtype in (("video", "video_tag"), ("audio", "audio_tag")):
        for tag in content_tag.find_all(tag_name):
            src = tag.get("src") or ""
            if src:
                _add(_abs_url(post_url, src), mtype)
            poster = tag.get("poster") or ""
            if poster and mtype == "video_tag":
                _add(_abs_url(post_url, poster), "video_poster")
            for source in tag.find_all("source"):
                src2 = source.get("src") or ""
                if src2:
                    _add(_abs_url(post_url, src2), mtype)

    # 본문 외부 <source> 단독 케이스는 드물지만 커버 (picture-less)
    for source in content_tag.find_all("source"):
        if source.find_parent(("video", "audio", "picture")):
            continue
        src = source.get("src") or ""
        if src:
            _add(_abs_url(post_url, src), "video_tag")

    # ── Cat D: 직접 미디어 확장자 앵커 ───────────────────────────────────
    for anchor in content_tag.find_all("a", href=True):
        if anchor.find_parent("div", class_="author-card"):
            continue
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:")):
            continue
        abs_href = _abs_url(post_url, href)
        if _is_embed_host(abs_href):
            continue
        ext = Path(urllib.parse.urlparse(abs_href).path).suffix.lower()
        if ext in MEDIA_EXTS:
            _add(abs_href, "anchor_direct")

    # ── Cat A: GDrive 오디오 (BGM/OST 컨텍스트) ─────────────────────────
    audio_gdrive_urls = _detect_non_image_urls(soup, post_url)
    for clean_gdrive_url in audio_gdrive_urls:
        # _detect_non_image_urls 는 clean_url 을 반환하므로 그대로 사용
        _add(clean_gdrive_url, "gdrive_audio")

    return results


def is_forum_era_post(soup: BeautifulSoup) -> bool:
    """현재 blog HTML 에 community-ko 포럼 썸네일 참조가 있으면 포럼 시대 포스트로 간주."""
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "community-ko.lordofheroes.com/storage/app/public/thumbnails/" in src:
            return True
    return False
