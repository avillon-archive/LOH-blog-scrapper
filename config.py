# -*- coding: utf-8 -*-
"""중앙 설정 — config.toml (없으면 config.default.toml) 에서 로드, 하드코딩 기본값 fallback."""

import re
import tomllib
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent

# ── TOML 로드 ─────────────────────────────────────────────────────────────
_cfg: dict = {}
for _candidate in ("config.toml", "config.default.toml"):
    try:
        with open(_PROJECT_ROOT / _candidate, "rb") as _f:
            _cfg = tomllib.load(_f)
        break
    except FileNotFoundError:
        continue

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
    "drive.google.com", "docs.google.com", "lh3.googleusercontent.com",
]))
SKIP_LINK_HOSTS: set[str] = set(_urls.get("skip_link_hosts", [
    "forms.gle", "forms.google.com", "play.google.com",
    "apps.apple.com", "go.onelink.me",
]))

# CDN
COMMUNITY_CDN_HOST: str = _cdn.get("community", "community-ko-cdn.lordofheroes.com")
GAME_CDN_HOST: str = _cdn.get("game", "cdn.clovergames.io")

# Kakao
KAKAO_PF_PROFILE: str = _kakao.get("profile_id", "_YXZqxb")
KAKAO_PF_API: str = (
    f"https://pf.kakao.com/rocket-web/web/profiles/{KAKAO_PF_PROFILE}/posts"
)

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

# MULTILANG_CONFIGS — build_posts_list.py 에서 사용하는 구조 그대로 생성
MULTILANG_CONFIGS: dict[str, dict] = {}
for _lang, _host in MULTILANG_BLOG_HOSTS.items():
    _base = f"https://{_host}"
    MULTILANG_CONFIGS[_lang] = {
        "sitemap_posts": f"{_base}/sitemap-posts.xml",
        "sitemap_pages": f"{_base}/sitemap-pages.xml",
        "all_posts": ROOT_DIR / f"all_posts_{_lang}.txt",
        "all_pages": ROOT_DIR / f"all_pages_{_lang}.txt",
        "all_links": ROOT_DIR / f"all_links_{_lang}.txt",
        "html_dir": ROOT_DIR / f"html_{_lang}",
        "done_html": ROOT_DIR / f"done_html_{_lang}.txt",
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

DL_KEYWORDS: set[str] = set(_file_types.get("dl_keywords", [
    "다운로드", "download", "다운", "받기", "저장",
    "고화질 이미지", "고화질", "이미지", "원본",
]))
NON_IMAGE_CONTEXT_KEYWORDS: set[str] = set(_file_types.get(
    "non_image_context_keywords",
    ["bgm", "ost", "음악", "사운드트랙", "soundtrack"],
))
