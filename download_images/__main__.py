# -*- coding: utf-8 -*-
"""CLI entry point: python -m download_images."""

import argparse

from utils import ROOT_DIR, load_posts

from . import backfill_image_map, run_images
from .process import _reprocess_fallbacks_cleanup


def main():
    parser = argparse.ArgumentParser(description="Image downloader")
    parser.add_argument("--retry", action="store_true", help="Retry failed list")
    parser.add_argument("--retry-multilang", action="store_true",
                        help="KakaoPF 성공 이미지에 multilang alt 보충")
    parser.add_argument("--retry-kakaopf", action="store_true",
                        help="multilang 성공 이미지에 KakaoPF alt 보충")
    parser.add_argument("--reprocess-fallbacks", action="store_true",
                        help="원본 재시도: 기존 multilang/kakao 폴백 이미지를 원본으로 교체 시도")
    parser.add_argument("--posts", default=str(ROOT_DIR / "all_links.txt"),
                        help="Posts list file")
    parser.add_argument("--backfill-map", action="store_true",
                        help="Backfill image_map.tsv")
    args = parser.parse_args()

    if args.backfill_map:
        backfill_image_map()
    elif args.reprocess_fallbacks:
        cleaned = _reprocess_fallbacks_cleanup()
        if cleaned:
            posts = load_posts(args.posts)
            run_images(posts, retry_mode=True, fallback_disabled=True)
    else:
        posts = load_posts(args.posts)
        run_images(posts, retry_mode=args.retry,
                   retry_multilang=args.retry_multilang,
                   retry_kakaopf=args.retry_kakaopf)


if __name__ == "__main__":
    main()
