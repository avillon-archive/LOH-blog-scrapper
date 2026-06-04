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
_output_dir = _paths["output_dir"]
_output_path = Path(_output_dir)
ROOT_DIR: Path = _output_path if _output_path.is_absolute() else _PROJECT_ROOT / _output_path

# ── 네트워크 ──────────────────────────────────────────────────────────────
DEFAULT_MAX_WORKERS: int = _network["default_max_workers"]
BLOG_RATE_LIMIT: float = _network["blog_rate_limit"]
BLOG_RATE_LIMIT_SMALL: float = _network["blog_rate_limit_small"]
DEFAULT_TIMEOUT: int = _network["default_timeout"]
RETRY_DELAYS: list[int] = _network["retry_delays"]
MAX_RETRIES: int = _network["max_retries"]

# ── URL / 도메인 ─────────────────────────────────────────────────────────
BLOG_HOST: str = _urls["blog_host"]
BLOG_BASE: str = f"https://{BLOG_HOST}"
BLOG_IMAGE_PREFIX: str = f"{BLOG_BASE}/content/images/"
SITEMAP_URL: str = f"{BLOG_BASE}/sitemap-posts.xml"
SITEMAP_PAGES_URL: str = f"{BLOG_BASE}/sitemap-pages.xml"
BLOG_HOST_RE = re.compile(
    rf"^https?://{re.escape(BLOG_HOST)}(/.*)?$", re.IGNORECASE,
)

WAYBACK_CDX_API: str = _urls["wayback_cdx_api"]
GDRIVE_HOSTS: set[str] = set(_urls["gdrive_hosts"])


def is_gdrive_host(hostname: str) -> bool:
    """Google Drive/이미지 호스트 판별. lhN.googleusercontent.com을 일반화 처리."""
    h = (hostname or "").lower()
    return h in GDRIVE_HOSTS or h.endswith(".googleusercontent.com")
SKIP_LINK_HOSTS: set[str] = set(_urls["skip_link_hosts"])

# CDN
COMMUNITY_CDN_HOST: str = _cdn["community"]
COMMUNITY_SITE_HOST: str = _cdn["community_site"]
GAME_CDN_HOST: str = _cdn["game"]

# Kakao
KAKAO_PF_PROFILE: str = _kakao["profile_id"]
KAKAO_PF_API: str = (
    f"https://pf.kakao.com/rocket-web/web/profiles/{KAKAO_PF_PROFILE}/posts"
)
KAKAO_TITLE_SIMILARITY: float = _kakao["title_similarity_threshold"]

# ── 다국어 ────────────────────────────────────────────────────────────────
# 값 출처는 config.default.toml [urls.multilang.*] 단일.
MULTILANG_BLOG_HOSTS: dict[str, str] = {}
MULTILANG_EARLIEST_DATE: dict[str, str] = {}

for _lang, _lang_cfg in _multilang.items():
    MULTILANG_BLOG_HOSTS[_lang] = _lang_cfg["blog_host"]
    MULTILANG_EARLIEST_DATE[_lang] = _lang_cfg["earliest_date"]

# 라이브 ghost host (현 ko primary + 다국어). cross-lang /content/ 이미지를 라이브로 복구할 때
# swap 대상(소유 언어 ghost host 가 /content/… 를 자기 storage 로 301). 죽은 구도메인 대응.
LIVE_BLOG_HOSTS: list[str] = [_urls["ghost_host"]]
for _lang_cfg in _multilang.values():
    _gh = _lang_cfg.get("ghost_host")
    if _gh and _gh not in LIVE_BLOG_HOSTS:
        LIVE_BLOG_HOSTS.append(_gh)

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
VALID_CATEGORIES: frozenset[str] = frozenset(_categories["valid"])

# tags 섹션에서 파생. 값 출처는 config.default.toml [categories.tags.*] 단일.
TAG_SLUG_TO_CATEGORY: dict[str, str] = {
    slug: info["ko"] for slug, info in _tags.items()
}

KO_TO_LANG_CAT: dict[str, dict[str, str]] = {"en": {}, "ja": {}}
for _slug, _info in _tags.items():
    _ko = _info["ko"]
    for _lang in ("en", "ja"):
        if _lang in _info:
            KO_TO_LANG_CAT[_lang][_ko] = _info[_lang]

# EN/JA 잔존 태그 정규화. 값 출처는 config.default.toml [categories.normalize.*] 단일.
EN_CAT_NORMALIZE: dict[str, str] = _normalize["en"]
JA_CAT_NORMALIZE: dict[str, str] = _normalize["ja"]

# ── 파일 타입 ─────────────────────────────────────────────────────────────
IMG_EXTS: set[str] = set(_file_types["img_exts"])
ARCHIVE_EXTS: set[str] = set(_file_types["archive_exts"])
DOWNLOADABLE_EXTS: set[str] = IMG_EXTS | ARCHIVE_EXTS

VIDEO_EXTS: set[str] = set(_file_types["video_exts"])
AUDIO_EXTS: set[str] = set(_file_types["audio_exts"])
MEDIA_EXTS: set[str] = VIDEO_EXTS | AUDIO_EXTS

DL_KEYWORDS: set[str] = set(_file_types["dl_keywords"])
NON_IMAGE_CONTEXT_KEYWORDS: set[str] = set(_file_types["non_image_context_keywords"])
