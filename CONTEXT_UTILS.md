# utils.py

## 주요 상수·함수

| 이름 | 설명 |
|------|------|
| `ROOT_DIR` | `Path(__file__).parent / "loh_blog"` — 모든 출력 데이터의 루트 디렉터리. 각 모듈이 import하여 사용 |
| `BLOG_HOST` | `"blog-ko.lordofheroes.com"` |
| `BLOG_RATE_LIMIT = 10.0` | 대규모 배치(>100건) 초당 최대 요청 |
| `BLOG_RATE_LIMIT_SMALL = 20.0` | 소규모 배치(≤100건) 초당 최대 요청 |
| `_TokenBucket` | 스레드 안전 토큰 버킷 rate limiter. `rate` req/s, `burst=2`. `fetch_with_retry`에서 블로그 도메인 요청 시 `acquire()` 호출 |
| `set_blog_rate_limit(rate)` | 런타임 rate limit 동적 변경. `_blog_rate_limiter`를 새 `_TokenBucket`으로 교체 |
| `REQUEST_DELAY` | *(제거됨)* → `_TokenBucket`로 대체 |
| `DEFAULT_MAX_WORKERS = 8` | ThreadPoolExecutor 기본 워커 수 |
| `USER_AGENT` | 공통 User-Agent |
| `VALID_CATEGORIES` | 유효 카테고리 frozenset (10개) |
| `extract_category(soup)` | 첫 번째 article:tag meta → 유효 카테고리 문자열 또는 `""` |
| `url_to_slug(post_url)` | URL 마지막 경로 세그먼트 → slug (최대 120자). `download_md`·`download_html` 공통 사용 |
| `get_session()` | 스레드 로컬 `requests.Session` 반환 |
| `fetch_with_retry(url, ...) -> requests.Response \| None` | 3회 재시도 + 백오프(1→2초). 404/410 즉시 포기. HTTP 429는 Retry-After를 존중하며 retry 횟수를 소모하지 않음. 블로그 도메인 요청은 `_blog_rate_limiter.acquire()` 통과 |
| `clean_url(url)` | `/size/w\d+` 제거 + `rstrip('/')` |
| `date_to_folder(date_str)` | `'YYYY-MM-DD'` → `'YYYY/MM'` |
| `load_image_map(filepath)` | `image_map.tsv` → `{clean_url: 상대경로}` |
| `load_done_file(filepath)` | `done_*.txt` → `{slug: post_url}` |
| `load_failed_post_urls(filepath)` | 실패 목록 첫 번째 컬럼 → `set[str]` |
| `load_posts(filepath)` | `all_links.txt` / `all_posts.txt` 등 → `[(url, date), ...]` |
| `append_line(filepath, line)` | 줄 추가. 스레드 안전(`_file_lock`), 부모 디렉토리 자동 생성 |
| `filter_file_lines(filepath, keep_fn)` | `keep_fn(line) → bool` 기반 in-place 필터링. 스레드 안전 |
| `remove_lines_by_prefix(filepath, prefix)` | `filter_file_lines`에 위임 |
| `eta_str(done, total, start_time)` | 진행률 + **Elapsed(경과 시간)** 문자열. 형식: `[  N/TOTAL \| XX.X% \| Elapsed HH:MM:SS]` |
| `ensure_utf8_console()` | Windows 콘솔 UTF-8 강제 설정 |

---

## FailedLog 클래스

`download_md.py` / `download_html.py` 공통 실패 이력 관리. `(post_url, reason)` 2-tuple 기반.

```python
class FailedLog:
    def __init__(self, filepath: Path, lock: threading.Lock): ...
    def record(self, post_url: str, reason: str) -> None   # 중복 방지
    def remove(self, post_url: str) -> None                # 해당 URL 전체 삭제
    def load_post_urls(self) -> set[str]                   # retry 필터링용
```

lock 내부에서 캐시 갱신, lock 외부에서 파일 기록. `append_line` 내부 `_file_lock`이 원자성 보장.

---

## write_text_unique 함수

`download_md.py` / `download_html.py` 공통의 slug 충돌 해소 + 파일 저장 패턴.

```python
def write_text_unique(
    target_dir, slug, suffix, content,
    done_map, done_urls, post_url, lock, done_file,
    force_overwrite=False,
) -> str | None:
```

1단계(잠금 외부): 동일 내용 기존 파일 탐색.
2단계(잠금 내부): 최종 경로 확정·쓰기·`done_map`/`done_urls` 갱신.
반환: 실제 slug 문자열. `post_url`이 already-done이면 `None`.
`force_overwrite=True`: 동일 slug 파일 존재 시 내용이 달라도 `_2` suffix 없이 덮어쓴다 (`--custom` 모드용).
`OSError`는 호출 측(`process_post`)에서 `write_failed:...`로 실패 기록 후 `False` 반환.

---

## LineBuffer 클래스

스레드 안전한 지연 flush 파일 버퍼. `download_images.py`의 고빈도 파일(downloaded_urls.txt, image_map.tsv 등)에 사용.

```python
class LineBuffer:
    def __init__(self, filepath: Path, flush_every: int = 100): ...
    def add(self, line: str) -> None       # 버퍼에 추가, flush_every 초과 시 자동 flush
    def flush_all(self) -> None            # 잔량 일괄 기록 (run 종료 시 필수 호출)
```

모듈 수준 `append_line`과 달리 `_file_lock`을 경유하지 않으므로 `_state_lock`/`_save_lock`과 경합하지 않는다.

---

## run_pipeline 함수

`download_md.py` / `download_html.py` 공통 ThreadPoolExecutor 루프.

```python
def run_pipeline(
    posts, process_fn, failed_log, retry_mode, label, max_workers
) -> None:
```

`process_fn: (url: str, date: str) -> bool`. retry 모드 시 실패 목록 필터링 및 성공 후 `failed_log.remove()` 처리.
진행도 출력 간격: 대상 포스트 수가 **100개 이하면 10개 단위**, **초과면 50개 단위**.
`download_images.py`는 retry 로직이 더 복잡(3-tuple, fetch_post_failed 별도 삭제)하므로 독립 구현 유지하며 동일한 출력 간격 규칙을 적용한다.

---

## HTML 인덱스·캐시 함수

파이프라인 간 HTML 재활용을 위한 유틸리티.

| 함수 | 설명 |
|------|------|
| `build_html_index(html_dir, done_file) -> dict[str, Path]` | `done_html.txt`를 기반으로 `{post_url: html_path}` 인덱스 구축. `html_dir` 내 `*.html` 파일을 rglob으로 스캔 |
| `fetch_post_html(url, html_index) -> str \| None` | `html_index`에서 로컬 HTML 파일 우선 조회. 없으면 `fetch_with_retry`로 서버에서 fetch |

`run_all.py`에서 HTML 단계 완료 후 `build_html_index()`를 호출하고, 반환된 인덱스를 images/md 단계에 `html_index` 파라미터로 전달한다.

---

# build_posts_list.py

## 사이트맵 URL

| 상수 | URL |
|------|-----|
| `SITEMAP_URL` | `https://blog-ko.lordofheroes.com/sitemap-posts.xml` |
| `SITEMAP_PAGES_URL` | `https://blog-ko.lordofheroes.com/sitemap-pages.xml` |

두 사이트맵 모두 `xml.etree.ElementTree`로 파싱 (namespace 유무 모두 처리).
`<lastmod>`에서 `YYYY-MM-DD` 추출. 없으면 빈 문자열로 맨 뒤 정렬.
가장 오래된 포스트: 2020-07-27.

## 출력 파일

| 상수 | 파일 | 설명 |
|------|------|------|
| `OUTPUT_FILE` | `loh_blog/all_posts.txt` | sitemap-posts.xml 결과, 날짜 내림차순 |
| `PAGES_OUTPUT_FILE` | `loh_blog/all_pages.txt` | sitemap-pages.xml 결과, 날짜 내림차순 |
| `LINKS_OUTPUT_FILE` | `loh_blog/all_links.txt` | 두 파일 병합·중복 제거, 날짜 내림차순 |

## 주요 함수

| 함수 | 설명 |
|------|------|
| `fetch_sitemap(url) -> str` | 사이트맵 XML fetch. 3회 재시도. 실패 시 `ConnectionError` 발생 |
| `parse_sitemap(xml) -> list[tuple[str, str]]` | XML 파싱 → `[(loc, date), ...]` 반환 |
| `_build_sitemap_file(sitemap_url, output_file)` | (내부 헬퍼) 범용 단일 사이트맵 fetch → 정렬 → 파일 저장. `build_and_write` / `build_pages_and_write` 공통 |
| `fetch_newest_single_sitemap_date(sitemap_url) -> str` | 단일 사이트맵 URL에서 최신 lastmod 날짜 반환. `_maybe_refresh_single()`에서 사용. 실패 시 `""` |
| `fetch_newest_sitemap_date() -> str` | posts + pages 두 사이트맵에서 가장 최신 lastmod 날짜를 반환. `fetch_newest_single_sitemap_date()` 호출 후 최대값. 둘 다 실패 시 `""` |
| `build_and_write() -> tuple[int, list[tuple[str, str]]]` | sitemap-posts.xml → `all_posts.txt` 전체 재작성. `(URL 수, entries 리스트)` 반환 |
| `build_pages_and_write() -> tuple[int, list[tuple[str, str]]]` | sitemap-pages.xml → `all_pages.txt` 전체 재작성. 시그니처·반환값은 `build_and_write()`와 동일 |
| `build_links_and_write() -> int` | `all_posts.txt` + `all_pages.txt` 병합·중복 제거 → `all_links.txt` 재작성. posts 항목 우선(동일 URL 시 posts 날짜 사용). 저장된 항목 수 반환 |

`main()`은 `build_and_write()` → `build_pages_and_write()` → `build_links_and_write()` 순서로 세 파일 모두 생성한다.
