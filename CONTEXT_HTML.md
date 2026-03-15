# download_html.py

## 경로 상수

`ROOT_DIR = Path(__file__).parent / "loh_blog"` (스크립트 위치 기준, 실행 디렉토리 무관).

## 락 구조

- `_html_done_lock`: `done_map` / `done_urls` 갱신 전용
- `_html_fail_lock`: `_failed_log` 내부 캐시 보호 전용

## 처리 흐름

fetch 후 Content-Type 검증(`text/html` 아니면 `unexpected_content_type:...`으로 실패).
`extract_category(soup)`로 카테고리 추출 후 `HTML_DIR / category` 또는 `HTML_DIR`에 저장.
`write_text_unique` 호출을 `try/except OSError`로 감싸고, 오류 시 `write_failed:...`로 실패 기록.
`process_post`가 `date`를 사용하지 않으므로 `run_pipeline` 호출 시 lambda로 시그니처를 맞춘다.

## run_html 시그니처

```python
def run_html(
    posts: list[tuple[str, str]],
    retry_mode: bool = False,
    force_download: bool = False,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> None:
```

- `force_download=True`: `done_urls`를 빈 set으로 초기화하여 기완료 포스트도 재다운로드. `write_text_unique`에 `force_overwrite=True` 전달.
- HTML 파이프라인은 `html_index`를 받지 않는다 (자신이 원본 HTML을 생성하는 첫 단계이므로).
