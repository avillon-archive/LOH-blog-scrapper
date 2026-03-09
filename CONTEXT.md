# 로드 오브 히어로즈 블로그 스크래퍼

`blog-ko.lordofheroes.com` 전체 포스트(사이트맵 기준 약 2,200개)의 이미지·MD·HTML을 로컬에 저장하는 Python 스크래퍼.

---

## 인코딩 설정

모든 파일: **UTF-8 (BOM 없음, LF 줄 끝)**. `.editorconfig`가 강제 적용.

---

## 파일 구조

| 파일 | 역할 |
|------|------|
| `utils.py` | 공통 유틸 (세션, 재시도, 파일 I/O, `url_to_slug`, `VALID_CATEGORIES`, `extract_category`, `FailedLog`, `write_text_unique`, `run_pipeline`) |
| `download_images.py` | 이미지 다운로드 (`ImageFailedLog` 클래스로 실패 이력 관리) |
| `download_md.py` | HTML → MD 변환·저장 |
| `download_html.py` | 원문 HTML 저장 |
| `run_all.py` | 마스터 실행 스크립트 (실행 전 `all_posts.txt` 자동 갱신 포함) |
| `build_posts_list.py` | 사이트맵 파싱 → `loh_blog/all_posts.txt` 생성 |
| `loh_blog/all_posts.txt` | 포스트 URL+날짜 (`URL\tYYYY-MM-DD`, 날짜 **내림차순**) |
| `requirements.txt` | `requests`, `beautifulsoup4`, `lxml` |

> **모듈 독립성**: `download_html.py`와 `download_md.py`는 서로 의존하지 않는다. `url_to_slug`는 `utils.py`에 정의되어 있으며 두 모듈이 공통 import한다.

---

## 실행 방법

```bash
pip install requests beautifulsoup4 lxml

python3 run_all.py                      # images + md + html 전체 (all_posts.txt 자동 갱신)
python3 run_all.py --images             # 이미지만
python3 run_all.py --md                 # MD만
python3 run_all.py --html               # HTML만
python3 run_all.py --retry              # 실패 목록 재처리
python3 run_all.py --sample 10          # 랜덤 10개 테스트
python3 run_all.py --sample 10 --retry  # 실패 목록에서 10개

python3 build_posts_list.py             # all_posts.txt 수동 재생성
```

파이프라인 실행 순서: `images → md → html` 고정.

---

## 저장 구조

```
./loh_blog/
  all_posts.txt              ← 포스트 URL+날짜 목록 (날짜 내림차순)
  images/YYYY/MM/            ← 본문 이미지 (날짜별 폴더)
  images/thumbnails/         ← og:image 썸네일
  md/                        ← 카테고리 없는 MD 파일
  md/카테고리명/              ← 카테고리별 MD 파일
  html/                      ← 카테고리 없는 원문 HTML
  html/카테고리명/            ← 카테고리별 원문 HTML
  downloaded_urls.txt        ← 이미지 URL 완료 이력 (main:/thumb: prefix)
  done_posts_images.txt      ← 이미지 완료 포스트 URL 목록
  image_map.tsv              ← clean_url → images/... 상대경로 (ROOT_DIR 기준)
  thumbnail_hashes.txt       ← 썸네일 SHA-256 해시 캐시
  done_md.txt                ← MD 완료 이력 (slug\tpost_url)
  done_html.txt              ← HTML 완료 이력 (slug\tpost_url)
  failed_images.txt          ← 이미지 실패 이력 (post_url\timg_url\treason)
  failed_md.txt              ← MD 실패 이력 (post_url\treason)
  failed_html.txt            ← HTML 실패 이력 (post_url\treason)
```

MD 파일 내 이미지 참조는 MD 파일 위치 기준 상대경로. `img_prefix`는 `process_post`에서 `target_dir`의 ROOT_DIR 기준 depth로 자동 계산된다.

| MD 파일 위치 | depth | img_prefix | 실제 경로 |
|---|---|---|---|
| `md/slug.md` | 1 | `../` | `../images/YYYY/MM/x.png` |
| `md/카테고리/slug.md` | 2 | `../../` | `../../images/YYYY/MM/x.png` |

`image_map.tsv`에 없는 이미지는 절대 URL로 폴백. 썸네일(`og_image`)은 `image_map.tsv`에 기록하지 않으며 URL-파일명 매핑이 없다.

---

## 카테고리 시스템 (`utils.py`)

`VALID_CATEGORIES`: 유효 카테고리 frozenset.
`["공지사항", "이벤트", "갤러리", "유니버스", "아발론서고", "쿠폰", "아발론 이벤트", "Special", "가이드", "확률 정보"]`

`extract_category(soup) -> str`: `<meta property="article:tag">` 중 **첫 번째** content 값을 읽어 `VALID_CATEGORIES`에 속하면 반환, 아니면 `""` 반환.

카테고리가 있는 포스트의 MD 헤더 형식:
```
# 제목
**작성일:** YYYY-MM-DD
**카테고리:** 카테고리명
**원문:** URL

---
```
카테고리가 없는 포스트는 `**카테고리:**` 행 없이 `**작성일:**` → `**원문:**` 순서.

---

## utils.py

### 주요 상수·함수

| 이름 | 설명 |
|------|------|
| `REQUEST_DELAY = 0.2` | HTTP 요청 성공 후 대기(초) |
| `DEFAULT_MAX_WORKERS = 8` | ThreadPoolExecutor 기본 워커 수 |
| `USER_AGENT` | 공통 User-Agent |
| `VALID_CATEGORIES` | 유효 카테고리 frozenset (10개) |
| `extract_category(soup)` | 첫 번째 article:tag meta → 유효 카테고리 문자열 또는 `""` |
| `url_to_slug(post_url)` | URL 마지막 경로 세그먼트 → slug (최대 120자). `download_md`·`download_html` 공통 사용 |
| `get_session()` | 스레드 로컬 `requests.Session` 반환 |
| `fetch_with_retry(url, ...) -> requests.Response \| None` | 3회 재시도 + 백오프(1→2초). 404/410 즉시 포기. 성공 시 `REQUEST_DELAY` 대기 |
| `clean_url(url)` | `/size/w\d+` 제거 + `rstrip('/')` |
| `date_to_folder(date_str)` | `'YYYY-MM-DD'` → `'YYYY/MM'` |
| `load_image_map(filepath)` | `image_map.tsv` → `{clean_url: 상대경로}` |
| `load_done_file(filepath)` | `done_*.txt` → `{slug: post_url}` |
| `load_failed_post_urls(filepath)` | 실패 목록 첫 번째 컬럼 → `set[str]` |
| `load_posts(filepath)` | `all_posts.txt` → `[(url, date), ...]` |
| `append_line(filepath, line)` | 줄 추가. 스레드 안전, 부모 디렉토리 자동 생성 |
| `filter_file_lines(filepath, keep_fn)` | `keep_fn(line) → bool` 기반 in-place 필터링. 스레드 안전 |
| `remove_lines_by_prefix(filepath, prefix)` | `filter_file_lines`에 위임 |
| `eta_str(done, total, start_time)` | 진행률+ETA 문자열 |
| `ensure_utf8_console()` | Windows 콘솔 UTF-8 강제 설정 |

### FailedLog 클래스

`download_md.py` / `download_html.py` 공통 실패 이력 관리. `(post_url, reason)` 2-tuple 기반.

```python
class FailedLog:
    def __init__(self, filepath: Path, lock: threading.Lock): ...
    def record(self, post_url: str, reason: str) -> None   # 중복 방지
    def remove(self, post_url: str) -> None                # 해당 URL 전체 삭제
    def load_post_urls(self) -> set[str]                   # retry 필터링용
```

lock 내부에서 캐시 갱신, lock 외부에서 파일 기록. `append_line` 내부 `_file_lock`이 원자성 보장.

### write_text_unique 함수

`download_md.py` / `download_html.py` 공통의 slug 충돌 해소 + 파일 저장 패턴.

```python
def write_text_unique(
    target_dir, slug, suffix, content,
    done_map, done_urls, post_url, lock, done_file
) -> str | None:
```

1단계(잠금 외부): 동일 내용 기존 파일 탐색.
2단계(잠금 내부): 최종 경로 확정·쓰기·`done_map`/`done_urls` 갱신.
반환: 실제 slug 문자열. `post_url`이 already-done이면 `None`.
`OSError`는 호출 측(`process_post`)에서 `write_failed:...`로 실패 기록 후 `False` 반환.

### run_pipeline 함수

`download_md.py` / `download_html.py` 공통 ThreadPoolExecutor 루프.

```python
def run_pipeline(
    posts, process_fn, failed_log, retry_mode, label, max_workers
) -> None:
```

`process_fn: (url: str, date: str) -> bool`. retry 모드 시 실패 목록 필터링 및 성공 후 `failed_log.remove()` 처리.
`download_images.py`는 retry 로직이 더 복잡(3-tuple, fetch_post_failed 별도 삭제)하므로 독립 구현 유지.

---

## build_posts_list.py

`https://blog-ko.lordofheroes.com/sitemap-posts.xml`을 `xml.etree.ElementTree`로 파싱 (namespace 유무 모두 처리).
`<lastmod>`에서 `YYYY-MM-DD` 추출. 없으면 빈 문자열로 맨 뒤 정렬.
출력 파일: `ROOT_DIR(=loh_blog/) / all_posts.txt`. 날짜 **내림차순** 정렬.
가장 오래된 포스트: 2020-07-27.

### 공개 함수

| 함수 | 설명 |
|------|------|
| `build_and_write() -> tuple[int, list[tuple[str, str]]]` | 사이트맵 파싱 후 `all_posts.txt` 전체 재작성. `(URL 수, entries 리스트)` 반환. `run_all.py`에서 import해 사용. |
| `fetch_newest_sitemap_date() -> str` | 사이트맵에서 가장 최신 lastmod 날짜만 반환. 실패 시 `""`. |

---

## download_images.py

### 경로 상수

`ROOT_DIR = Path(__file__).parent / "loh_blog"` (스크립트 위치 기준, 실행 디렉토리 무관).

### 주요 상수

- `BLOG_HOST = "blog-ko.lordofheroes.com"`
- `GDRIVE_HOSTS = {"drive.google.com", "docs.google.com", "lh3.googleusercontent.com"}`
- `COMMUNITY_CDN_HOST = "community-ko-cdn.lordofheroes.com"`
- `DONE_POSTS_FILE`: 이미지 수집 완료 포스트 URL 목록.

### 락 구조

`_dl_lock` 단일 락이 `seen_urls` / `og_hashes` / `image_map` 갱신 및 파일 저장의 원자성을 보장.

### ImageFailedLog 클래스

이미지 실패 이력(3-tuple: `post_url`, `img_url`, `reason`)을 스레드 안전하게 관리.
`utils.FailedLog`와 동일한 "lock 내부 캐시 갱신 / lock 외부 파일 기록" 패턴 적용.
전역 `_failed_log` 인스턴스로 접근. 모듈 수준 `record_failed` / `remove_from_failed` 래퍼 함수 제공.

```python
class ImageFailedLog:
    def record(self, post_url, img_url, reason) -> None
    def remove(self, post_url, reason=None) -> None   # reason=None이면 post_url 전체 삭제
    def load_post_urls(self) -> set[str]
```

### 이미지 수집 (`collect_image_urls`)

| 소스 | 타입 | 조건 |
|------|------|------|
| `og:image` meta | `og_image` | 전체 페이지 |
| img src/data-src | `gdrive` | `hostname in GDRIVE_HOSTS` |
| img src/data-src | `img` | `"/content/images/" in path and hostname == BLOG_HOST` |
| img src/data-src | `img` | `hostname == COMMUNITY_CDN_HOST and ext in IMG_EXTS` |
| a href | `gdrive` | `hostname in GDRIVE_HOSTS` |
| a href | `linked_direct` | `ext in IMG_EXTS` |
| a href | `linked_keyword` | 앵커 텍스트에 다운로드 키워드 또는 해상도 패턴(`\d+[xX×]\d+`) |

content_tag 탐색 순서: `.gh-content` → `.post-content` → `article` → `main`.

### 다운로드 폴백 체인

| 타입 | 1단계 | 2단계 | 3단계 |
|------|-------|-------|-------|
| `img` / `og_image` | 직접 + CT 검증 | Wayback `im_` | Wayback 포스트 스냅샷에서 img/og:image 탐색 |
| `gdrive` | 직접 (min 500B) | Wayback `im_` | Wayback 포스트 스냅샷에서 img 탐색 |
| `linked_keyword` | 직접 + CT 검증 | Wayback `im_` | Wayback 포스트 스냅샷에서 `<a>` 탐색 |
| `linked_direct` | 직접 + CT 또는 확장자 | community CDN에 한해 Wayback `im_` | - |

- `_wayback_oldest(url)`: CDX API `limit=1`. `_wayback_cache`에 캐시. 동일 URL 동시 요청 시 `_wayback_events`로 선착 스레드만 fetch, 나머지는 대기 후 캐시 사용.
- `_add_im(url)`: `/web/{ts}/` → `/web/{ts}im_/`.
- Wayback 포스트 스냅샷 탐색 함수들은 `_normalized_link_key` 완전 일치 비교.

### 기타

- `save_image(content, filename, folder) -> str`: 충돌 시 `_2`, `_3` 번호 부여. 항상 유효한 파일명 반환.
- `failed_images.txt` 형식: `post_url\timg_url\treason`. fetch_post_failed는 img_url 빈 문자열.
- retry 모드: `fetch_post_failed`는 포스트 fetch 성공 시 제거. `download_failed`는 `fail==0 and ok>0` 시에만 제거.
- `--backfill-map`: 기존 다운로드 이력으로 `image_map.tsv` 재구성. thumbnails 폴더는 resolved path 비교로 제외.
- 썸네일 해시 캐시: 시작 시 파일이 있으면 로드, 없으면 thumbnails 폴더 전체 스캔 후 생성 (최초 1회).
- `image_map.tsv`에 썸네일(`og_image`) 경로는 기록하지 않는다. 썸네일은 hash 기반 중복 제거만 수행하며 URL-파일명 매핑이 없다.

---

## download_md.py

### 경로 상수

`ROOT_DIR = Path(__file__).parent / "loh_blog"` (스크립트 위치 기준).

### 락 구조

- `_md_done_lock`: `done_map` / `done_urls` 갱신 전용
- `_md_fail_lock`: `_failed_log` 내부 캐시 보호 전용

### HTML → Markdown 변환

- **제목 탐색**: `h1.post-title` → `h1` → `og:title` 순.
- **본문 탐색**: `section.post-content` → `div.post-content` → `article` → `main` 순.
- **제거 태그**: `author-card`, `post-share`, `post-tags`, `post-nav`, `related-posts`, `comments`.
- **제목 중복 방지**: body 내 h1 sweep 후 `title_tag.parent is not None` 체크로 header 범위 외 제목 별도 제거.
- **`_wrap_marker(inner, marker)`**: `**`, `*`, `~~` 마커를 씌울 때 앞뒤 공백을 마커 바깥으로 이동. whitespace-only인 경우 마커 없이 원문 공백을 그대로 반환 (중첩 strong 평탄화 시 공백 소멸 방지).
- **`_strip_marker(text, marker)`**: non-greedy 매칭으로 text 내 중첩된 동일 마커 래핑을 모두 제거. `strong/b`, `em/i`, `del/s/strike` 변환 시 `_children_inline` 결과에 적용 후 `_wrap_marker`를 씌운다. 원본 HTML에 잘못 중첩된 `<strong><strong>...</strong></strong>` 구조를 단일 `**...**`로 평탄화한다.
- **`img_to_md(img_tag, post_url, image_map, img_prefix)`**: `image_map` 등록 시 `img_prefix + 상대경로` 형태로 참조. `img_prefix`는 `process_post`에서 `target_dir.relative_to(ROOT_DIR).parts`의 depth로 자동 계산 (`md/` → `"../"`, `md/카테고리/` → `"../../"`). 미등록 시 절대 URL 폴백.
- `INLINE_MAX_DEPTH = 60`: 비정상 중첩 HTML 안전장치.
- `collapse_blank_lines`: 연속 빈 줄 최대 1개.
- `_convert_table`: `<thead>` 없이 `<tbody>`만 있을 때 첫 tr을 헤더로 사용하며 body_rows 중복 방지.
- slug 충돌 시 `write_text_unique`가 `slug_2.md`, `slug_3.md` ... 자동 처리.
- `OSError` 발생 시 `write_failed:...`로 실패 기록 후 `False` 반환.

---

## download_html.py

### 경로 상수

`ROOT_DIR = Path(__file__).parent / "loh_blog"` (스크립트 위치 기준).

### 락 구조

- `_html_done_lock`: `done_map` / `done_urls` 갱신 전용
- `_html_fail_lock`: `_failed_log` 내부 캐시 보호 전용

### 처리 흐름

fetch 후 Content-Type 검증(`text/html` 아니면 `unexpected_content_type:...`으로 실패).
`extract_category(soup)`로 카테고리 추출 후 `HTML_DIR / category` 또는 `HTML_DIR`에 저장.
`write_text_unique` 호출을 `try/except OSError`로 감싸고, 오류 시 `write_failed:...`로 실패 기록.
`process_post`가 `date`를 사용하지 않으므로 `run_pipeline` 호출 시 lambda로 시그니처를 맞춘다.

---

## run_all.py

- `POSTS_FILE = ROOT_DIR / "all_posts.txt"` (`ROOT_DIR = Path(__file__).parent / "loh_blog"`)
- 실행 순서: `PIPELINE_ORDER = ("images", "md", "html")` 고정.
- `--retry --sample` 조합 시 세 실패 파일의 union에서 샘플링.

### all_posts.txt 자동 갱신

파이프라인 실행 직전 `_maybe_refresh_posts_list()` 호출.

1. `_newest_local_date(POSTS_FILE)`: `all_posts.txt` 첫 줄의 날짜(내림차순 최신) 읽기.
2. `fetch_newest_sitemap_date()`: 사이트맵에서 최신 날짜 취득.
3. 두 값이 일치하면 갱신 스킵. 불일치하거나 로컬 파일이 없으면 `build_and_write()`로 전체 재빌드.

---

## 네트워크 설정

Claude 컨테이너 환경은 네트워크 활성화 상태. 단, 허용된 도메인 목록(`api.anthropic.com`, `github.com`, `pypi.org` 등)만 접근 가능하며, `blog-ko.lordofheroes.com` 및 `web.archive.org`는 허용 목록에 포함되지 않는다. 의존성 설치(`pip install`)는 컨테이너 내에서 직접 수행 가능하고, 실제 스크래핑은 허용 도메인이 포함된 로컬 환경에서 수행한다.
