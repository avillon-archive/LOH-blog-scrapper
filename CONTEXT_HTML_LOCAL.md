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

## 아키텍처

### CssDownloader

CSS 다운로드·캐싱 담당 클래스. `run_html_local`에서 1회 생성, 모든 스레드가 공유.

- **per-URL 락**: 같은 URL의 동시 다운로드 방지 (전역 락이 아니라 URL별 Lock).
- **파일명 해시**: `{stem}_{md5[:8]}.{ext}` 형식으로 다른 경로의 동명 CSS 충돌 방지.
- CSS 내 `url()` 상대경로는 절대 URL로 정규화.

### HtmlLocalizer

BeautifulSoup 기반 HTML 변환 클래스. 포스트 1건당 인스턴스 생성.

`localize()` 호출 시 아래 순서로 변환 적용 후 HTML 문자열 반환:

1. **CSS 로컬화** (`_localize_css`): `<link rel="stylesheet">` href를 `CssDownloader`로 다운로드한 로컬 경로로 교체.
2. **이미지 리라이트** (`_rewrite_images`, `_rewrite_meta_images`, `_rewrite_style_bg_images`): `image_map.tsv` 기반. `clean_url()`로 키 생성 후 매핑 조회.
   - **매핑 성공**: `src`를 로컬 상대경로로 교체, `srcset`·`data-src` 제거.
   - **매핑 실패**: 절대 URL로 정규화.
   - `<style>` 블록·인라인 `style`의 `background-image: url(...)`, `<meta og:image>`, `<link rel="icon">`, JSON-LD 내 이미지도 처리.
3. **내부 링크 로컬화** (`_rewrite_internal_links`): `slug_map` 기반. 블로그 내부 `<a href>`를 파일 간 상대경로로 리라이트. `tag/`, `author/` 등은 `_EXCLUDED_PATHS`로 건너뜀.
4. **JS 제거** (`_remove_scripts`): 모든 `<script>` 태그 제거. `<noscript>` 유지.

### _process_post

`HtmlLocalizer`를 사용하는 단일 포스트 처리 함수. HTML 읽기 → 카테고리 결정 → `localizer.localize()` → 파일 저장. done 추적은 `process_fn` 클로저가 담당.

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

- `done_lock`: `process_fn` 클로저에서 done_slugs/done_urls/done_file 갱신 보호.
- `CssDownloader._lock` + `_url_locks`: per-URL 락으로 CSS 다운로드 경합 방지.

## utils.py 재사용

- `load_done_file()`: done_html_local.txt 파싱.
- `build_html_index()`: done_html.txt + html/ 디렉토리에서 소스 목록 구축.

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

- `force_download=True`: `done_urls` 빈 set으로 초기화하여 전체 재생성.
- KO 전용. EN/JA는 대상 아님.

## run_all.py 통합

- `--html-local` 플래그로 단독 실행 가능.
- `PIPELINE_ORDER`에 `"html-local"` 포함 (md 뒤). 인자 미지정 시 전체 파이프라인에 포함.
- `--retry`, `--force` 지원.
