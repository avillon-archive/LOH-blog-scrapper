# download_html.py

## 경로 상수

`ROOT_DIR`은 `utils.py`에서 import (`from utils import ROOT_DIR`).

## 락 구조

모듈 레벨 `_html_done_lock`, `_html_fail_lock`, `_failed_log`는 하위 호환용 (기본 KO 경로 `process_post()` 래퍼에서 사용). `run_html()` 호출 시에는 함수 내부에서 독립 락·FailedLog 인스턴스를 생성한다.

## 처리 흐름

fetch 후 Content-Type 검증(`text/html` 아니면 `unexpected_content_type:...`으로 실패).
`extract_category(soup)`로 카테고리 추출 후 `html_dir / category` 또는 `html_dir`에 저장.
`write_text_unique` 호출을 `try/except OSError`로 감싸고, 오류 시 `write_failed:...`로 실패 기록.

## run_html 시그니처

```python
def run_html(
    posts: list[tuple[str, str]],
    retry_mode: bool = False,
    force_download: bool = False,
    max_workers: int = DEFAULT_MAX_WORKERS,
    html_dir: Path = HTML_DIR,
    done_file: Path = DONE_FILE,
    failed_file: Path = FAILED_FILE,
) -> None:
```

- `html_dir`, `done_file`, `failed_file`: 기본값은 KO 경로. EN/JA 다운로드 시 `html_en/`, `done_html_en.txt` 등을 전달.
- `force_download=True`: `done_urls`를 빈 set으로 초기화하여 기완료 포스트도 재다운로드. `write_text_unique`에 `force_overwrite=True` 전달.
- HTML 파이프라인은 `html_index`를 받지 않는다 (자신이 원본 HTML을 생성하는 첫 단계이므로).
- EN/JA HTML은 `extract_category()`가 `""` 반환 → `html_dir` 루트에 flat 저장.

## process_post (하위 호환 래퍼)

```python
def process_post(post_url, done_slugs, done_urls, force_overwrite=False) -> bool:
```

기본 KO 경로(`HTML_DIR`, `DONE_FILE`)를 사용하는 래퍼. 내부적으로 `_process_post()`를 호출한다.
