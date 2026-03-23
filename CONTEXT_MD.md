# download_md.py

## 경로 상수

`ROOT_DIR`은 `utils.py`에서 import (`from utils import ROOT_DIR`).

## 락 구조

- `_md_done_lock`: `done_map` / `done_urls` 갱신 전용
- `_md_fail_lock`: `_failed_log` 내부 캐시 보호 전용

## HTML → Markdown 변환

`markitdown` 패키지(`MarkItDown`)를 사용하며, 스레드별 인스턴스를 `_thread_local`로 캐싱한다 (`_get_converter()`).

- **날짜 추출**: `<meta property="article:published_time">` 에서 `YYYY-MM-DD`를 우선 추출하고, 없으면 `post_date` 인자를 fallback으로 사용한다. 따라서 `custom_posts.txt`처럼 날짜 컬럼이 없어도 HTML에서 자동으로 날짜를 파싱한다.
- **제목 탐색**: `h1.post-title` → `h1` → `og:title` 순. (`_TITLE_CLASS_RE` 사전 컴파일 정규식)
- **본문 탐색**: `section.post-content` → `div.post-content` → `article` → `main` 순. (`_BODY_CLASS_RE` 사전 컴파일 정규식)
- **제거 태그**: `author-card`, `post-share`, `post-tags`, `post-nav`, `related-posts`, `comments`. (`_UNWANTED_CLASS_RE` 단일 정규식으로 일괄 탐색)
- **제목 중복 방지**: body 내 h1 sweep 후 `title_tag.parent is not None` 체크로 header 범위 외 제목 별도 제거.
- **`_flatten_nested_inline(body)`**: 변환 전 전처리. `strong/b`, `em/i`, `del/s/strike` 태그가 동일 태그로 중첩(`<strong><strong>...</strong></strong>`)되어 있으면 내부 태그를 `unwrap()`하여 평탄화한다. 원본 HTML의 중첩 오류가 markitdown에서 `**********text**********` 처럼 마커 누적으로 출력되는 것을 방지한다.
- **`_rewrite_images(body, ...)`**: `image_map` 등록 시 `img_prefix + 상대경로` 형태로 참조. `img_prefix`는 `process_post`에서 `target_dir.relative_to(ROOT_DIR).parts`의 depth로 자동 계산 (`md/` → `"../"`, `md/카테고리/` → `"../../"`). 미등록 시 절대 URL 폴백.
- **`_resolve_links(body, post_url)`**: 상대 `<a href>` 를 절대 URL로 변환.
- slug 충돌 시 `write_text_unique`가 `slug_2.md`, `slug_3.md` ... 자동 처리.
- `OSError` 발생 시 `write_failed:...`로 실패 기록 후 `False` 반환.

## run_md 시그니처

```python
def run_md(
    posts: list[tuple[str, str]],
    retry_mode: bool = False,
    force_download: bool = False,
    html_index: dict[str, Path] | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> None:
```

- `force_download=True`: `done_urls` 빈 set으로 초기화 + `write_text_unique`에 `force_overwrite=True` 전달.
- `html_index`: `fetch_post_html(url, html_index)`를 통해 로컬 HTML 우선 조회. `run_all.py`의 HTML 단계에서 구축한 인덱스를 전달받는다.
- `image_map`: `IMAGE_MAP_FILE`에서 로드. 이미지 참조를 상대경로로 변환하는 데 사용.
