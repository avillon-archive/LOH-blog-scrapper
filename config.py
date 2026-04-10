# -*- coding: utf-8 -*-
"""중앙 설정 — config.default.toml 을 base 로 로드하고, config.toml 이 있으면 deep-merge 로 override."""

import re
import tomllib
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent


def _deep_merge(base: dict, override: dict) -> dict:
    """재귀 dict 병합. override 가 base 의 동일 키를 덮어쓴다.

    중첩 dict 는 재귀적으로 병합 ([network] 에서 한 필드만 덮어써도 나머지 유지).
    list/scalar 는 통째로 교체.
    """
    for key, value in override.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


# ── TOML 로드 ─────────────────────────────────────────────────────────────
# config.default.toml 은 필수(리포 동봉). config.toml 은 선택(사용자 override).
_default_path = _PROJECT_ROOT / "config.default.toml"
try:
    with open(_default_path, "rb") as _f:
        _cfg: dict = tomllib.load(_f)
except FileNotFoundError as _e:
    raise FileNotFoundError(
        f"config.default.toml 이 없다: {_default_path}. 리포에 포함되어야 한다."
    ) from _e

_override_path = _PROJECT_ROOT / "config.toml"
if _override_path.exists():
    with open(_override_path, "rb") as _f:
        _override_cfg = tomllib.load(_f)
    _deep_merge(_cfg, _override_cfg)

_paths = _cfg.get("paths", {})
_network = _cfg.get("network", {})
_urls = _cfg.get("urls", {})
_cdn = _urls.get("cdn", {})
_kakao = _urls.get("kakao", {})
_multilang = _urls.get("multilang", {})
_categories = _cfg.get("categories", {})
_tags = _categories.get("tags", {})
_normalize = _categories.get("normalize", {})
_file_types = _cfg.get("file_types", {})

# ── 경로 ──────────────────────────────────────────────────────────────────
_output_dir = _paths.get("output_dir", "loh_blog")
_output_path = Path(_output_dir)
ROOT_DIR: Path = _output_path if _output_path.is_absolute() else _PROJECT_ROOT / _output_path

# ── 네트워크 ──────────────────────────────────────────────────────────────
DEFAULT_MAX_WORKERS: int = _network.get("default_max_workers", 8)
BLOG_RATE_LIMIT: float = _network.get("blog_rate_limit", 10.0)
BLOG_RATE_LIMIT_SMALL: float = _network.get("blog_rate_limit_small", 20.0)
DEFAULT_TIMEOUT: int = _network.get("default_timeout", 20)
RETRY_DELAYS: list[int] = _network.get("retry_delays", [1, 2])
MAX_RETRIES: int = _network.get("max_retries", 3)

# ── URL / 도메인 ─────────────────────────────────────────────────────────
BLOG_HOST: str = _urls.get("blog_host", "blog-ko.lordofheroes.com")
BLOG_BASE: str = f"https://{BLOG_HOST}"
BLOG_IMAGE_PREFIX: str = f"{BLOG_BASE}/content/images/"
SITEMAP_URL: str = f"{BLOG_BASE}/sitemap-posts.xml"
SITEMAP_PAGES_URL: str = f"{BLOG_BASE}/sitemap-pages.xml"
BLOG_HOST_RE = re.compile(
    rf"^https?://{re.escape(BLOG_HOST)}(/.*)?$", re.IGNORECASE,
)

WAYBACK_CDX_API: str = _urls.get(
    "wayback_cdx_api", "https://web.archive.org/cdx/search/cdx",
)
GDRIVE_HOSTS: set[str] = set(_urls.get("gdrive_hosts", [
    "drive.google.com", "docs.google.com",
]))


def is_gdrive_host(hostname: str) -> bool:
    """Google Drive/이미지 호스트 판별. lhN.googleusercontent.com을 일반화 처리."""
    h = (hostname or "").lower()
    return h in GDRIVE_HOSTS or h.endswith(".googleusercontent.com")
SKIP_LINK_HOSTS: set[str] = set(_urls.get("skip_link_hosts", [
    "forms.gle", "forms.google.com", "play.google.com",
    "apps.apple.com", "go.onelink.me",
]))

# CDN
COMMUNITY_CDN_HOST: str = _cdn.get("community", "community-ko-cdn.lordofheroes.com")
COMMUNITY_SITE_HOST: str = _cdn.get("community_site", "community-ko.lordofheroes.com")
GAME_CDN_HOST: str = _cdn.get("game", "cdn.clovergames.io")

# Kakao
KAKAO_PF_PROFILE: str = _kakao.get("profile_id", "_YXZqxb")
KAKAO_PF_API: str = (
    f"https://pf.kakao.com/rocket-web/web/profiles/{KAKAO_PF_PROFILE}/posts"
)
KAKAO_TITLE_SIMILARITY: float = _kakao.get("title_similarity_threshold", 0.55)

# ── 다국어 ────────────────────────────────────────────────────────────────
_DEFAULT_MULTILANG = {
    "en": {"blog_host": "blog-en.lordofheroes.com", "earliest_date": "2020-10-20"},
    "ja": {"blog_host": "blog-ja.lordofheroes.com", "earliest_date": "2021-01-15"},
}

MULTILANG_BLOG_HOSTS: dict[str, str] = {}
MULTILANG_EARLIEST_DATE: dict[str, str] = {}

for _lang, _defaults in _DEFAULT_MULTILANG.items():
    _lang_cfg = _multilang.get(_lang, {})
    MULTILANG_BLOG_HOSTS[_lang] = _lang_cfg.get("blog_host", _defaults["blog_host"])
    MULTILANG_EARLIEST_DATE[_lang] = _lang_cfg.get(
        "earliest_date", _defaults["earliest_date"],
    )

# ── 이미지 오버라이드 ────────────────────────────────────────────────────
IMAGE_OVERRIDES: dict[str, str] = _cfg.get("image_overrides", {})

# ── 미디어 원격 리라이트 (gdrive → R2 등) ────────────────────────────────
# 죽은 원본 URL → 외부 R2/CDN URL. --media 수집 자체를 스킵하고
# download_html_local 이 HTML 의 해당 URL 을 R2 URL 로 치환한다.
# base URL + 엔트리별 상대 경로 로 최종 URL 을 구성한다.
_media_remote = _cfg.get("media_remote", {})
_rewrite_base_raw: str = (_media_remote.get("base") or "").strip()
MEDIA_REMOTE_REWRITE_BASE: str = (
    _rewrite_base_raw.rstrip("/") + "/" if _rewrite_base_raw else ""
)
_rewrite_entries: dict[str, str] = _media_remote.get("rewrites", {}) or {}
if _rewrite_entries and not MEDIA_REMOTE_REWRITE_BASE:
    raise ValueError(
        "[media_remote.rewrites] 엔트리가 있지만 [media_remote].base 가 비어 있다. "
        "config.toml 에 media_remote.base 를 지정하라."
    )
MEDIA_REMOTE_REWRITES: dict[str, str] = {
    k: MEDIA_REMOTE_REWRITE_BASE + v.lstrip("/")
    for k, v in _rewrite_entries.items()
}

# MULTILANG_CONFIGS — build_posts_list.py 에서 사용하는 구조 그대로 생성
MULTILANG_CONFIGS: dict[str, dict] = {}
for _lang, _host in MULTILANG_BLOG_HOSTS.items():
    _base = f"https://{_host}"
    MULTILANG_CONFIGS[_lang] = {
        "sitemap_posts": f"{_base}/sitemap-posts.xml",
        "sitemap_pages": f"{_base}/sitemap-pages.xml",
        "all_posts": ROOT_DIR / f"all_posts_{_lang}.csv",
        "all_pages": ROOT_DIR / f"all_pages_{_lang}.csv",
        "all_links": ROOT_DIR / f"all_links_{_lang}.csv",
        "html_dir": ROOT_DIR / f"html_{_lang}",
        "done_html": ROOT_DIR / f"done_html_{_lang}.csv",
    }

# ── 카테고리 ──────────────────────────────────────────────────────────────
VALID_CATEGORIES: frozenset[str] = frozenset(_categories.get("valid", [
    "공지사항", "이벤트", "갤러리", "유니버스", "아발론서고",
    "쿠폰", "아발론 이벤트", "Special", "가이드", "확률 정보",
]))

# tags 섹션에서 파생
_DEFAULT_TAGS: dict[str, dict[str, str]] = {
    "notices":  {"ko": "공지사항", "en": "Notice",   "ja": "お知らせ"},
    "events":   {"ko": "이벤트",   "en": "Event",    "ja": "イベント"},
    "gallery":  {"ko": "갤러리",   "en": "Gallery",  "ja": "ユニバース"},
    "universe": {"ko": "유니버스", "en": "Universe",  "ja": "英雄紹介"},
    "library":  {"ko": "아발론서고", "en": "Gallery",  "ja": "ユニバース"},
    "coupon":   {"ko": "쿠폰",    "en": "Coupon",    "ja": "クーポン"},
}
_resolved_tags = _tags if _tags else _DEFAULT_TAGS

TAG_SLUG_TO_CATEGORY: dict[str, str] = {
    slug: info["ko"] for slug, info in _resolved_tags.items()
}

KO_TO_LANG_CAT: dict[str, dict[str, str]] = {"en": {}, "ja": {}}
for _slug, _info in _resolved_tags.items():
    _ko = _info["ko"]
    for _lang in ("en", "ja"):
        if _lang in _info:
            KO_TO_LANG_CAT[_lang][_ko] = _info[_lang]

# EN/JA 잔존 태그 정규화
_DEFAULT_EN_NORMALIZE = {"New Hero": "Universe", "avillontoon": "Gallery"}
_DEFAULT_JA_NORMALIZE = {"漫画": "ユニバース", "Event-Completed": "イベント"}

EN_CAT_NORMALIZE: dict[str, str] = _normalize.get("en", _DEFAULT_EN_NORMALIZE)
JA_CAT_NORMALIZE: dict[str, str] = _normalize.get("ja", _DEFAULT_JA_NORMALIZE)

# ── 파일 타입 ─────────────────────────────────────────────────────────────
IMG_EXTS: set[str] = set(_file_types.get("img_exts", [
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
]))
ARCHIVE_EXTS: set[str] = set(_file_types.get("archive_exts", [
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz",
]))
DOWNLOADABLE_EXTS: set[str] = IMG_EXTS | ARCHIVE_EXTS

VIDEO_EXTS: set[str] = set(_file_types.get("video_exts", [
    ".mp4", ".webm", ".mov", ".mkv", ".m4v",
]))
AUDIO_EXTS: set[str] = set(_file_types.get("audio_exts", [
    ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac",
]))
MEDIA_EXTS: set[str] = VIDEO_EXTS | AUDIO_EXTS

DL_KEYWORDS: set[str] = set(_file_types.get("dl_keywords", [
    "다운로드", "download", "다운", "받기", "저장",
    "고화질 이미지", "고화질", "이미지", "원본",
]))
NON_IMAGE_CONTEXT_KEYWORDS: set[str] = set(_file_types.get(
    "non_image_context_keywords",
    ["bgm", "ost", "음악", "사운드트랙", "soundtrack"],
))
