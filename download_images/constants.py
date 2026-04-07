# -*- coding: utf-8 -*-
"""경로, 정규식, 상수 집합."""

import re

from utils import BLOG_HOST, ROOT_DIR, SIZE_W_RE  # noqa: F401 – re-export

IMAGES_DIR = ROOT_DIR / "images"
DONE_FILE = ROOT_DIR / "downloaded_urls.txt"
DONE_POSTS_FILE = ROOT_DIR / "done_posts_images.txt"
FAILED_FILE = ROOT_DIR / "failed_images.txt"
IMAGE_MAP_FILE = ROOT_DIR / "image_map.tsv"
THUMB_HASH_FILE = ROOT_DIR / "thumbnail_hashes.txt"
IMG_HASH_FILE = ROOT_DIR / "image_hashes.tsv"
MULTILANG_LOG_FILE = IMAGES_DIR / "multilang_fallback.tsv"
MULTILANG_INDEX_CACHE = ROOT_DIR / "multilang_sitemap_index.json"
KAKAO_PF_LOG_FILE = IMAGES_DIR / "kakao_pf_log.tsv"
KAKAO_PF_INDEX_FILE = ROOT_DIR / "kakao_pf_index.json"
FALLBACK_REPORT_FILE = ROOT_DIR / "fallback_report.csv"

IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}
DOWNLOADABLE_EXTS = IMG_EXTS | ARCHIVE_EXTS
DL_KEYWORDS = {
    "다운로드", "download", "다운", "받기", "저장",
    "고화질 이미지", "고화질", "이미지", "원본",
}
RESOLUTION_RE = re.compile(r"\d+\s*[xX×]\s*\d+")
GDRIVE_HOSTS = {"drive.google.com", "docs.google.com", "lh3.googleusercontent.com"}
COMMUNITY_CDN_HOST = "community-ko-cdn.lordofheroes.com"
WAYBACK_CDX_API = "https://web.archive.org/cdx/search/cdx"

_SKIP_LINK_HOSTS = {
    "forms.gle", "forms.google.com", "play.google.com", "apps.apple.com",
    "go.onelink.me",
}

_NON_IMAGE_CONTEXT_KEYWORDS = {"bgm", "ost", "음악", "사운드트랙", "soundtrack"}

MULTILANG_BLOG_HOSTS = {
    "en": "blog-en.lordofheroes.com",
    "ja": "blog-ja.lordofheroes.com",
}
MULTILANG_EARLIEST_DATE = {"en": "2020-10-20", "ja": "2021-01-15"}
_KO_SUFFIX_RE = re.compile(r"(?i)_ko(?=[\._\-])")
_LANG_SUFFIX_MAP = {"en": "_EN", "ja": "_JP"}

KAKAO_PF_PROFILE = "_YXZqxb"
KAKAO_PF_API = f"https://pf.kakao.com/rocket-web/web/profiles/{KAKAO_PF_PROFILE}/posts"

_ARCHIVE_CONTENT_TYPES = {
    "application/zip", "application/x-zip-compressed",
    "application/x-rar-compressed", "application/x-7z-compressed",
    "application/gzip", "application/x-tar",
    "application/octet-stream",
}
