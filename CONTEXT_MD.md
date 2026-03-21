# download_md.py

## 경로 상수

`ROOT_DIR`은 `utils.py`에서 import (`from utils import ROOT_DIR`).

## 락 구조

- `_md_done_lock`: `done_map` / `done_urls` 갱신 전용
- `_md_fail_lock`: `_failed_log` 내부 캐시 보호 전용

## HTML → Markdown 변환

- **제목 탐색**: `h1.post-title` → `h1` → `og:title` 순. (`_TITLE_CLASS_RE` 사전 컴파일 정규식)
- **본문 탐색**: `section.post-content` → `div.post-content` → `article` → `main` 순. (`_BODY_CLASS_RE` 사전 컴파일 정규식)
- **제거 태그**: `author-card`, `post-share`, `post-tags`, `post-nav`, `related-posts`, `comments`. (`_UNWANTED_CLASS_RE` 단일 정규식으로 일괄 탐색)
- **제목 중복 방지**: body 내 h1 sweep 후 `title_tag.parent is not None` 체크로 header 범위 외 제목 별도 제거.
- **`_wrap_marker(inner, marker)`**: `**`, `*`, `~~` 마커를 씌울 때 앞뒤 공백을 마커 바깥으로 이동. whitespace-only인 경우 마커 없이 원문 공백을 그대로 반환 (중첩 strong 평탄화 시 공백 소멸 방지).
- **`_strip_marker(text, marker)`**: text가 해당 마커로 외부 래핑된 경우에만 마커를 제거한다 (중첩 마커 평탄화용). 마커 문자 경계를 직접 검사하므로 `**bold**` 내부에서 `*`를 오탐하지 않는다. `strong/b`, `em/i`, `del/s/strike` 변환 시 `_children_inline` 결과에 적용 후 `_wrap_marker`를 씌운다. 원본 HTML에 잘못 중첩된 `<strong><strong>...</strong></strong>` 구조를 단일 `**...**`로 평탄화한다.
- **`img_to_md(img_tag, post_url, image_map, img_prefix)`**: `image_map` 등록 시 `img_prefix + 상대경로` 형태로 참조. `img_prefix`는 `process_post`에서 `target_dir.relative_to(ROOT_DIR).parts`의 depth로 자동 계산 (`md/` → `"../"`, `md/카테고리/` → `"../../"`). 미등록 시 절대 URL 폴백.
- `INLINE_MAX_DEPTH = 60`: 비정상 중첩 HTML 안전장치.
- `collapse_blank_lines`: 연속 빈 줄 최대 1개.
- `_convert_table`: `<thead>` 없이 `<tbody>`만 있을 때 첫 tr을 헤더로 사용하며 body_rows 중복 방지.
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
