# -*- coding: utf-8 -*-
"""CLI entry point: python -m download_images."""

import argparse

from utils import ROOT_DIR, load_posts

from . import backfill_image_map, run_fallback_images, run_images


def main():
    parser = argparse.ArgumentParser(description="Image downloader")
    parser.add_argument("--retry", action="store_true", help="Retry failed list")
    parser.add_argument("--retry-fallback", action="store_true",
                        help="실패 이미지에 multilang/kakao 폴백 시도 (별도 디렉토리에 보존)")
    parser.add_argument("--posts", default=str(ROOT_DIR / "all_links.csv"),
                        help="Posts list file")
    parser.add_argument("--backfill-map", action="store_true",
                        help="Backfill image_map.csv")
    args = parser.parse_args()

    if args.backfill_map:
        backfill_image_map()
    elif args.retry_fallback:
        posts = load_posts(args.posts)
        run_fallback_images(posts)
    else:
        posts = load_posts(args.posts)
        run_images(posts, retry_mode=args.retry)


if __name__ == "__main__":
    main()
