# -*- coding: utf-8 -*-
"""포스트 단위 처리, 폴백 재처리, alt 보충, rename."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bs4 import BeautifulSoup

from utils import (
    DEFAULT_MAX_WORKERS,
    ROOT_DIR,
    clean_url,
    date_to_folder,
    extract_category,
    fetch_post_html,
)

from .collect import _detect_non_image_urls, collect_image_urls
from .constants import (
    DONE_FILE,
    DONE_POSTS_FILE,
    FAILED_FILE,
    IMAGE_MAP_FILE,
    IMAGES_DIR,
    IMG_HASH_FILE,
    KAKAO_PF_LOG_FILE,
    MULTILANG_LOG_FILE,
)
from .download import (
    _determine_filename,
    _save_alternative_image,
    _source_tag,
    download_one_image,
)
from .fallback_kakao import KakaoPFPost, _build_kakao_pf_index
from .fallback_multilang import (
    _build_multilang_date_index,
    _fetch_multilang_wayback_image,
)
from .fallback_kakao import _fetch_kakao_pf_image
from .models import PostProcessResult
from .persistence import (
    record_failed,
    remove_from_failed,
    remove_from_failed_batch,
)
from .state import (
    _done_posts_buf,
    _kakao_pf_log_buf,
    _multilang_log_buf,
)
from .url_utils import _clean_img_url, _strip_ref_param


# ---------------------------------------------------------------------------
# 포스트 단위 처리
# ---------------------------------------------------------------------------


def process_post(
    post_url: str,
    post_date: str,
    seen_urls: set[str],
    img_hashes: dict[str, str],
    image_map: dict[str, str],
    thumb_hashes: set[str],
    done_post_urls: dict[str, int],
    html_index: "dict[str, Path] | None" = None,
    *,
    retry_mode: bool = False,
    multilang_date_index: dict[str, list[tuple[str, str]]] | None = None,
    kakao_pf_index: dict[str, list[KakaoPFPost]] | None = None,
    published_time: str = "",
) -> PostProcessResult:
    if post_url in done_post_urls and not retry_mode:
        return PostProcessResult(ok=0, fail=0, post_fetch_ok=True)

    html_text = fetch_post_html(post_url, html_index)
    if html_text is None:
        if post_url in done_post_urls:
            return PostProcessResult(ok=0, fail=0, post_fetch_ok=True)
        record_failed(post_url, "", "fetch_post_failed")
        return PostProcessResult(ok=0, fail=1, post_fetch_ok=False)

    soup = BeautifulSoup(html_text, "lxml")
    images = collect_image_urls(soup, post_url)

    if post_url in done_post_urls and len(images) == done_post_urls[post_url]:
        return PostProcessResult(ok=0, fail=0, post_fetch_ok=True)

    blog_title = ""
    if retry_mode and kakao_pf_index:
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            blog_title = title_tag.string.strip()
        else:
            h1 = soup.find("h1")
            if h1:
                blog_title = h1.get_text(strip=True)

    non_image_urls = _detect_non_image_urls(soup, post_url)
    if retry_mode:
        for skip_url in non_image_urls:
            remove_from_failed(post_url, img_url=skip_url)

    category = extract_category(soup)
    date_folder = date_to_folder(post_date)
    folder = IMAGES_DIR / (category or "etc") / date_folder

    ok = fail = ok_saved = ok_original = ok_multilang = ok_kakao = ok_dedup = 0
    succeeded_urls: list[str] = []
    post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] = {}
    for idx, (img_url, utype) in enumerate(images, start=1):
        if _clean_img_url(img_url) in non_image_urls:
            continue
        how = download_one_image(
            img_url, utype, post_url, folder, idx,
            seen_urls, img_hashes, image_map, thumb_hashes,
            post_soup_cache=post_soup_cache,
            post_date=post_date,
            blog_title=blog_title,
            retry_mode=retry_mode,
            multilang_date_index=multilang_date_index,
            kakao_pf_index=kakao_pf_index,
            published_time=published_time,
        )
        if how:
            ok += 1
            succeeded_urls.append(img_url)
            if how == "dup":
                ok_dedup += 1
            elif how == "already":
                pass
            else:
                ok_saved += 1
                if how == "original":
                    ok_original += 1
                elif how == "multilang":
                    ok_multilang += 1
                elif how == "kakao":
                    ok_kakao += 1
        else:
            fail += 1

    if fail == 0:
        done_post_urls[post_url] = len(images)
        _done_posts_buf.add(f"{post_url}\t{len(images)}")

    return PostProcessResult(ok=ok, fail=fail, post_fetch_ok=True,
                             ok_saved=ok_saved, ok_original=ok_original,
                             ok_multilang=ok_multilang, ok_kakao=ok_kakao,
                             ok_dedup=ok_dedup,
                             succeeded_urls=succeeded_urls)


# ---------------------------------------------------------------------------
# 폴백 이미지 재처리 (--reprocess-fallbacks)
# ---------------------------------------------------------------------------


def _filter_file(filepath: Path, keep) -> int:
    """파일의 각 줄에 대해 keep 함수가 True인 줄만 남기고 재작성한다.

    Returns:
        제거된 줄 수.
    """
    if not filepath.exists():
        return 0
    lines = filepath.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in lines if keep(ln)]
    removed = len(lines) - len(kept)
    if removed:
        filepath.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return removed


def _reprocess_fallbacks_cleanup() -> int:
    """multilang/kakao 폴백 로그를 읽어 트래킹 파일에서 해당 항목을 제거한다."""
    entries: list[tuple[str, str, str]] = []
    for log_file in (MULTILANG_LOG_FILE, KAKAO_PF_LOG_FILE):
        if not log_file.exists():
            continue
        for line in log_file.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split("\t")
            if len(parts) < 3:
                continue
            saved_path = parts[0]
            post_url = parts[1]
            original_img_url = parts[3] if len(parts) >= 4 else ""
            entries.append((saved_path, post_url, original_img_url))

    if not entries:
        print("[재처리] 폴백 로그에 항목이 없습니다.")
        return 0

    remove_seen: set[str] = set()
    for _, _, orig_url in entries:
        if orig_url:
            cleaned = clean_url(_strip_ref_param(orig_url))
            remove_seen.add(f"main:{cleaned}")
            remove_seen.add(f"thumb:{cleaned}")

    remove_paths = {e[0] for e in entries}
    remove_posts = {e[1] for e in entries}

    n = _filter_file(DONE_FILE, lambda ln: ln.strip() not in remove_seen)
    if n:
        print(f"  downloaded_urls.txt: {n}건 제거")

    n = _filter_file(
        IMG_HASH_FILE,
        lambda ln: ln.split("\t")[1].strip() not in remove_paths
        if "\t" in ln else True,
    )
    if n:
        print(f"  image_hashes.tsv: {n}건 제거")

    n = _filter_file(
        IMAGE_MAP_FILE,
        lambda ln: ln.split("\t")[1].strip() not in remove_paths
        if "\t" in ln else True,
    )
    if n:
        print(f"  image_map.tsv: {n}건 제거")

    n = _filter_file(
        DONE_POSTS_FILE,
        lambda ln: ln.split("\t")[0].strip() not in remove_posts,
    )
    if n:
        print(f"  done_posts_images.txt: {n}건 제거")

    added = 0
    with FAILED_FILE.open("a", encoding="utf-8") as f:
        for _, post_url, orig_url in entries:
            if orig_url:
                f.write(f"{post_url}\t{orig_url}\tdownload_failed\n")
                added += 1
    if added:
        print(f"  failed_images.txt: {added}건 추가")

    for log_file in (MULTILANG_LOG_FILE, KAKAO_PF_LOG_FILE):
        if log_file.exists():
            log_file.write_text("", encoding="utf-8")

    print(f"[재처리] {len(entries)}건 트래킹 제거 완료")
    return len(entries)


# ---------------------------------------------------------------------------
# 기존 폴백 이미지 rename (출처 접두사 부여)
# ---------------------------------------------------------------------------


def _rename_fallback_images() -> int:
    renamed = 0
    for log_file, default_tag in [
        (KAKAO_PF_LOG_FILE, "[Kakao]"),
        (MULTILANG_LOG_FILE, None),
    ]:
        if not log_file.exists():
            continue
        lines = log_file.read_text(encoding="utf-8").splitlines()
        new_lines: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                new_lines.append(line)
                continue
            rel_path, post_url, source = parts[0], parts[1], parts[2]

            basename = Path(rel_path).name
            if basename.startswith("["):
                new_lines.append(line)
                continue

            tag = default_tag if default_tag else _source_tag(source)
            if not tag:
                new_lines.append(line)
                continue

            from .url_utils import _safe_filename
            old_path = ROOT_DIR / rel_path
            new_name = f"{tag} {basename}"
            new_path = old_path.parent / _safe_filename(new_name)
            if old_path.exists() and not new_path.exists():
                old_path.rename(new_path)
                new_rel = new_path.relative_to(ROOT_DIR).as_posix()
                new_lines.append(f"{new_rel}\t{post_url}\t{source}")
                renamed += 1
            else:
                new_lines.append(line)

        log_file.write_text("\n".join(new_lines) + ("\n" if new_lines else ""),
                            encoding="utf-8")
    return renamed


# ---------------------------------------------------------------------------
# Alt 이미지 보충 (--retry-multilang / --retry-kakaopf)
# ---------------------------------------------------------------------------


def _supplement_alt_images(
    mode: str,
    posts: list[tuple[str, str]],
    html_index: "dict[str, Path] | None" = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> None:
    """한쪽 폴백만 성공한 이미지에 반대쪽 alt를 보충한다."""
    if mode == "multilang":
        src_log = KAKAO_PF_LOG_FILE
        dst_log_buf = _multilang_log_buf
        label = "multilang"
    elif mode == "kakaopf":
        src_log = MULTILANG_LOG_FILE
        dst_log_buf = _kakao_pf_log_buf
        label = "KakaoPF"
    else:
        print(f"[보충] 알 수 없는 모드: {mode}")
        return

    if not src_log.exists():
        print(f"[보충] 소스 로그 파일 없음: {src_log}")
        return

    log_entries: dict[str, list[str]] = {}
    for line in src_log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rel_path, post_url, _source = parts[0], parts[1], parts[2]
        log_entries.setdefault(post_url, []).append(rel_path)

    if not log_entries:
        print(f"[보충] 소스 로그에 항목 없음")
        return

    post_date_map = {url: date for url, date, *_ in posts}
    post_pub_map = {url: pub for url, _date, pub, *_ in posts if pub}
    target_posts = [(url, post_date_map.get(url, "")) for url in log_entries
                    if url in post_date_map]

    if not target_posts:
        print(f"[보충] 대상 포스트 없음")
        return

    print(f"[보충] {label} alt 보충 대상: {len(target_posts)}개 포스트")

    if mode == "multilang":
        print(f"[보충] 다국어 사이트맵 인덱스 구축 중...")
        multilang_date_index = _build_multilang_date_index()
        kakao_pf_index: dict[str, list[KakaoPFPost]] = {}
    else:
        print(f"[보충] Kakao PF 인덱스 구축 중...")
        multilang_date_index: dict[str, list[tuple[str, str]]] = {}
        kakao_pf_index = _build_kakao_pf_index()
        if kakao_pf_index:
            total_kp = sum(len(v) for v in kakao_pf_index.values())
            print(f"  Kakao PF 인덱스: {len(kakao_pf_index)}일, 총 {total_kp}개 포스트")

    start = time.time()
    total_supplemented = 0
    completed = 0

    def _process_one_post(post_url: str, post_date: str, published_time: str = "") -> int:
        html_text = fetch_post_html(post_url, html_index)
        if html_text is None:
            return 0

        soup = BeautifulSoup(html_text, "lxml")
        images = collect_image_urls(soup, post_url)

        blog_title = ""
        if mode == "kakaopf":
            title_tag = soup.find("title")
            if title_tag and title_tag.string:
                blog_title = title_tag.string.strip()
            else:
                h1 = soup.find("h1")
                if h1:
                    blog_title = h1.get_text(strip=True)

        category = extract_category(soup)
        date_folder = date_to_folder(post_date)
        if category:
            folder = IMAGES_DIR / category / date_folder
        else:
            folder = IMAGES_DIR / date_folder

        post_soup_cache: dict[str, tuple[BeautifulSoup, str] | None] = {}
        count = 0

        for idx, (img_url, utype) in enumerate(images, start=1):
            if utype == "linked_direct":
                continue

            if mode == "multilang":
                result = _fetch_multilang_wayback_image(
                    post_url, img_url, post_date, utype, idx,
                    multilang_date_index, post_soup_cache,
                    published_time=published_time,
                )
            else:
                result = _fetch_kakao_pf_image(
                    post_url, img_url, post_date, utype, idx,
                    kakao_pf_index, blog_title=blog_title,
                    published_time=published_time,
                )

            if result is None:
                continue
            alt_content = result[0]
            alt_source = result[4]
            tag = _source_tag(alt_source)
            alt_filename = _determine_filename(
                utype, img_url, result[1], result[2], result[3], idx
            )
            alt_rel = _save_alternative_image(alt_content, alt_filename, folder,
                                              source_tag=tag)
            if alt_rel:
                dst_log_buf.add(f"{alt_rel}\t{post_url}\t{alt_source}")
                count += 1

        return count

    report_interval = 10 if len(target_posts) <= 100 else 50
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_post = {
            executor.submit(_process_one_post, url, date, post_pub_map.get(url, "")): url
            for url, date in target_posts
        }
        for future in as_completed(future_to_post):
            try:
                count = future.result()
            except Exception as exc:
                post_url = future_to_post[future]
                print(f"  [오류] {post_url}: {exc}")
                count = 0
            total_supplemented += count
            completed += 1
            if completed % report_interval == 0 or completed == len(target_posts):
                elapsed = time.time() - start
                print(f"  {completed}/{len(target_posts)} "
                      f"({elapsed:.0f}s) 보충={total_supplemented}")

    dst_log_buf.flush_all()
    print(f"\n[보충 완료] {label} alt {total_supplemented}개 이미지 보충")
