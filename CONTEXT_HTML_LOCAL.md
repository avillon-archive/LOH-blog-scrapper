# download_html_local.py

## 목적

블로그 운영 중단 대비, 다운로드된 KO HTML을 오프라인 열람 가능하도록 후처리. 원본 `html/`은 보존하고 `html_local/`에 별도 저장.

## 경로 상수

- `HTML_DIR`: `loh_blog/html/` (소스)
- `HTML_LOCAL_DIR`: `loh_blog/html_local/` (출력)
- `ASSETS_DIR`: `loh_blog/html_local/assets/` (CSS 등 공용 에셋)
- `IMAGE_MAP_FILE`: `loh_blog/image_map.tsv`
- `DONE_FILE`: `loh_blog/done_html_local.txt`
- `FAILED_FILE`: `loh_blog/failed_html_local.txt`

## 처리 흐름

네트워크 요청은 CSS 다운로드 1회뿐. 나머지는 로컬 파일 변환.

1. **CSS 로컬화**: `<link rel="stylesheet">` → `assets/screen.css`에 다운로드, href를 상대경로로 교체. CSS 내부 `url()` 참조는 절대 URL로 정규화.
2. **이미지 리라이트**: `image_map.tsv` 기반. `clean_url()`로 키 생성 후 매핑 조회.
   - **매핑 성공**: `src`를 로컬 상대경로로 교체, `srcset` 제거, `data-src` 제거.
   - **매핑 실패**: `src`/`srcset`/`data-src` 모두 원본 URL 유지.
   - `<style>` 블록·인라인 `style`의 `background-image: url(...)`, `<meta og:image>`, `<link rel="icon">`, JSON-LD 내 이미지도 처리.
3. **내부 링크 로컬화**: `done_html.txt`에서 `slug_map` 구축. 블로그 내부 `<a href>`를 `html_local/` 파일 간 상대경로로 리라이트. `tag/`, `author/` 등 목록 페이지는 건너뜀.
4. **JS 제거**: 모든 `<script>` 태그 제거 (analytics, jQuery, casper.js 등). `<noscript>` 유지.
5. **저장**: 원본 `html/`과 동일한 카테고리 구조로 `html_local/`에 저장.

## 상대경로 산출

이미지:
- `html_local/{slug}.html` → `../images/...`
- `html_local/{category}/{slug}.html` → `../../images/...`

에셋:
- `html_local/{slug}.html` → `assets/...`
- `html_local/{category}/{slug}.html` → `../assets/...`

내부 링크:
- `html_local/{category}/A.html` → `../{other_category}/B.html`
- `html_local/A.html` → `{category}/B.html`

## 락 구조

`done_lock`: done_slugs/done_urls/done_file 갱신 보호. `_css_lock`: CSS 다운로드 중복 방지.

## run_html_local 시그니처

```python
def run_html_local(
    retry_mode: bool = False,
    force_download: bool = False,
    max_workers: int = DEFAULT_MAX_WORKERS,
    html_dir: Path = HTML_DIR,
    html_local_dir: Path = HTML_LOCAL_DIR,
    done_html_file: Path = ROOT_DIR / "done_html.txt",
    done_file: Path = DONE_FILE,
    failed_file: Path = FAILED_FILE,
) -> None:
```

- 소스: `done_html.txt` + `html/` 디렉토리에서 `(html_path, post_url)` 목록 구축. `posts` 인자를 받지 않음 (네트워크 다운로드가 아닌 로컬 변환이므로).
- `force_download=True`: `done_urls` 빈 set으로 초기화하여 전체 재생성.
- KO 전용. EN/JA는 대상 아님.

## run_all.py 통합

- `--html-local` 플래그로 단독 실행 가능.
- `PIPELINE_ORDER`에 `"html-local"` 포함 (md 뒤). 인자 미지정 시 전체 파이프라인에 포함.
- `--retry`, `--force` 지원.
