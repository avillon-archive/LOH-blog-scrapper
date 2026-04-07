# run_all.py

## 주요 상수

- `POSTS_FILE = ROOT_DIR / "all_posts.txt"`
- `PAGES_FILE = ROOT_DIR / "all_pages.txt"`
- `LINKS_FILE = ROOT_DIR / "all_links.txt"`
- `CUSTOM_POSTS_FILE = ROOT_DIR / "custom_posts.txt"`
- `ROOT_DIR`: `utils.py`에서 import
- 실행 순서: `PIPELINE_ORDER = ("html", "images", "md")` 고정.

## HTML 항상 실행

`selected = user_selected | {"html"}` — `--images`나 `--md`만 지정해도 HTML 단계는 항상 포함된다. 단, 사용자가 명시적으로 `--html`을 선택하지 않은 경우 HTML 단계에 `retry_mode`와 `force_download`를 전달하지 않는다 (보조 단계로만 실행).

## CLI 옵션

| 옵션 | 설명 |
|------|------|
| `--images` / `--md` / `--html` | 해당 단계만 실행. 미지정 시 전체 실행. (html은 항상 포함) |
| `--retry` | 실패 목록 재처리. `--sample`과 조합 가능. |
| `--posts` | `all_posts.txt`를 소스로 사용. 해당 사이트맵 개별 갱신 체크. |
| `--pages` | `all_pages.txt`를 소스로 사용. 해당 사이트맵 개별 갱신 체크. |
| `--custom` | `custom_posts.txt`를 소스로 사용. 사이트맵 갱신 건너뜀. |
| `--force` | 기존 기록 무시하고 전체 재다운로드 (`done` 기록 무시). |
| `--sample N` | 랜덤 N개 테스트. **`all_links.txt` 행 수의 10%를 상한**으로 자동 클램핑. `--posts` / `--pages` / `--custom`과 동시 사용 불가. |
| `--seed N` | `--sample` 샘플링 고정 시드. |
| *(미지정)* | `all_links.txt` 사용 (기본값). 사이트맵 freshness 체크 후 필요 시 갱신. |

`--posts`, `--pages`, `--custom`은 상호 배타적. 둘 이상 동시 지정 시 `parser.error()`.

## 주요 헬퍼 함수

| 함수 | 설명 |
|------|------|
| `_maybe_refresh_posts_list()` | `all_links.txt` 자동 갱신. `fetch_newest_sitemap_date()`(posts + pages 양쪽)로 remote 날짜 취득, `all_links.txt` 첫 줄 날짜와 비교해 불일치 시 `build_and_write()` → `build_pages_and_write()` → `build_links_and_write()` 순서로 재빌드. pages 갱신 실패 시에도 기존 `all_pages.txt`를 활용해 links 생성 시도. |
| `_maybe_refresh_single(posts_file, sitemap_url, build_fn, label)` | `--posts`/`--pages` 사용 시 해당 사이트맵만 개별 freshness 체크. `fetch_newest_single_sitemap_date(sitemap_url)`로 remote 날짜와 로컬 파일 첫 줄 비교, 불일치 시 `build_fn()` 호출. |
| `_newest_local_date(posts_file)` | 지정 파일의 첫 번째 유효 날짜 읽기. 파일 부재·`OSError` 모두 `""` 반환. |
| `_count_file_lines(posts_file)` | 파일의 유효 행 수(공백·`#` 제외) 반환. `--sample` 상한 계산 시 `LINKS_FILE` 기준으로 호출. 읽기 실패 시 `0`. |
| `_load_failed_posts_for_retry(selected)` | 선택된 단계의 실패 파일 union → `set[str]`. |
| `_sample_posts(posts, n, seed)` | 랜덤 샘플링. |
| `build_multilang_and_write()` | EN/JA sitemap-posts + sitemap-pages → `all_posts_{lang}.txt` + `all_pages_{lang}.txt` + `all_links_{lang}.txt` 생성. `MULTILANG_CONFIGS` dict 참조. |
| `_build_multilang_links(cfg)` | 개별 언어의 posts + pages → links 병합. |

## 사이트맵 갱신 흐름

**기본 모드 (소스 미지정)**:
1. `_newest_local_date(LINKS_FILE)`: `all_links.txt` 첫 줄의 날짜(내림차순 최신) 읽기.
2. `fetch_newest_sitemap_date()`: posts + pages 사이트맵 양쪽에서 최신 날짜 취득 (둘 중 최대값).
3. 두 값이 일치하면 갱신 스킵. 불일치하거나 로컬 파일이 없으면 `build_and_write()` → `build_pages_and_write()` → `build_links_and_write()`로 전체 재빌드.

**`--posts` / `--pages` 모드**:
1. `_maybe_refresh_single()` 호출로 해당 사이트맵만 개별 체크.
2. `fetch_newest_single_sitemap_date(sitemap_url)`로 remote 최신 날짜 취득.
3. 로컬 첫 줄 날짜와 비교하여 불일치 시 개별 파일만 재빌드.

**`--custom` 모드**: 사이트맵 갱신 건너뜀.

## 동적 워커·rate limit 설정

```
posts ≤ 100건: workers = min(len(posts), 32), rate_limit = 20 req/s
posts > 100건: workers = DEFAULT_MAX_WORKERS (8), rate_limit = 10 req/s
```

## --sample 상한 클램핑

```
cap = max(1, all_links.txt 유효 행 수 // 10)
args.sample = min(args.sample, cap)
```

`all_links.txt`가 없거나 읽기 실패 시 클램핑 없이 통과.

## 파이프라인 실행

```python
for step in selected_order:
    if step == "html":
        # 1) KO HTML 다운로드
        run_html(posts, retry_mode, force_download, max_workers)
        fill_published_times()
        fill_published_times(PAGES_FILE)
        build_links_and_write()

        # 2) EN/JA 포스트+페이지 목록 생성 + HTML 다운로드
        build_multilang_and_write()  # posts + pages + links per lang
        for lang, cfg in MULTILANG_CONFIGS.items():
            lang_links = load_posts(cfg["all_links"])
            run_html(lang_links, ..., html_dir=cfg["html_dir"],
                     done_file=cfg["done_html"], failed_file=...)
            fill_published_times(cfg["all_posts"], cfg["html_dir"], cfg["done_html"])
            fill_published_times(cfg["all_pages"], cfg["html_dir"], cfg["done_html"])

        # 3) KO + EN/JA 통합 html_index 구축
        html_index = build_html_index(HTML_DIR, DONE_HTML_FILE)
        for lang, cfg in MULTILANG_CONFIGS.items():
            html_index.update(build_html_index(cfg["html_dir"], cfg["done_html"]))
    elif step == "images":
        run_images(posts, ...)
    elif step == "md":
        run_md(posts, ...)
```

HTML 완료 후 `build_html_index()`를 KO/EN/JA 각각 호출하여 `{post_url: Path}` 인덱스를 merge, 이후 단계에 전달한다.
