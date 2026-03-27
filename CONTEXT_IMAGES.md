# download_images.py

## 경로 상수

`ROOT_DIR`은 `utils.py`에서 import (`from utils import ROOT_DIR`).

## 주요 상수

- `BLOG_HOST`: `utils.py`에서 import
- `GDRIVE_HOSTS = {"drive.google.com", "docs.google.com", "lh3.googleusercontent.com"}`
- `COMMUNITY_CDN_HOST = "community-ko-cdn.lordofheroes.com"`
- `DL_KEYWORDS = {"다운로드", "download", "다운", "받기", "저장", "고화질 이미지", "고화질", "이미지", "원본"}`
- `ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}` — 다운로드 가능한 압축 파일 확장자
- `DOWNLOADABLE_EXTS = IMG_EXTS | ARCHIVE_EXTS` — 이미지 + 압축 파일 통합
- `_SKIP_LINK_HOSTS` — 다운로드 대상이 아닌 외부 링크 도메인 (forms.gle, play.google.com 등)
- `_NON_IMAGE_CONTEXT_KEYWORDS` — 비이미지 다운로드 링크 감지용 키워드 (bgm, ost 등)
- `DONE_POSTS_FILE`: 이미지 수집 완료 포스트 URL+이미지 수 목록 (`URL\t이미지수`)
- `FALLBACK_REPORT_FILE`: 폴백 성공 CSV 리포트 (`post_url, original_img_url, fallback_type, saved_path, source_url`)

## 락 구조

- `_state_lock`: `seen_urls` / `img_hashes` / `image_map` / `thumb_hashes` in-memory 갱신 전용.
- `_save_lock`: `save_image()` 파일명 충돌 해소 전용 (디스크 I/O 직렬화).
- `_dl_lock`: `ImageFailedLog` 내부 캐시 전용.

## ImageFailedLog 클래스

이미지 실패 이력(3-tuple: `post_url`, `img_url`, `reason`)을 스레드 안전하게 관리.
`utils.FailedLog`와 동일한 "lock 내부 캐시 갱신 / lock 외부 파일 기록" 패턴 적용.
전역 `_failed_log` 인스턴스로 접근. 모듈 수준 `record_failed` / `remove_from_failed` 래퍼 함수 제공.

```python
class ImageFailedLog:
    def record(self, post_url, img_url, reason) -> None
    def remove(self, post_url, reason=None, img_url=None) -> None   # reason=None이면 post_url 전체 삭제, img_url 지정 시 해당 엔트리만 삭제
    def remove_batch(self, post_url, img_urls: set[str]) -> None    # 여러 img_url을 한 번의 파일 I/O로 일괄 제거
    def load_post_urls(self) -> set[str]
```

## 고빈도 파일용 LineBuffer 인스턴스

`append_line` 대신 `LineBuffer`를 사용해 파일 syscall을 대폭 감소시킨다.

| 인스턴스 | 파일 | 용도 |
|----------|------|------|
| `_done_buf` | `downloaded_urls.txt` | 이미지 URL 완료 이력 |
| `_map_buf` | `image_map.tsv` | clean_url → 상대경로 |
| `_img_hash_buf` | `image_hashes.tsv` | 해시 → 상대경로 |
| `_done_posts_buf` | `done_posts_images.txt` | 포스트 완료 이력 |
| `_multilang_log_buf` | `multilang_fallback.tsv` | 다국어 폴백 성공 로그 |
| `_kakao_pf_log_buf` | `kakao_pf_log.tsv` | Kakao PF 폴백 성공 로그 |

`run_images` 종료 시 모든 버퍼에 `flush_all()` 호출 필수.

---

## 이미지 수집 (`collect_image_urls`)

| 소스 | 타입 | 조건 |
|------|------|------|
| `og:image` meta | `og_image` | 전체 페이지 |
| img src/data-src | `gdrive` | `hostname in GDRIVE_HOSTS` |
| img src/data-src | `img` | `"/content/images/" in path and hostname == BLOG_HOST` |
| img src/data-src | `img` | `hostname == COMMUNITY_CDN_HOST and ext in IMG_EXTS` |
| a href | `gdrive` | `hostname in GDRIVE_HOSTS` |
| a href | `linked_direct` | `ext in DOWNLOADABLE_EXTS` |
| a href | `linked_keyword` | 앵커 텍스트에 `DL_KEYWORDS` 키워드 또는 해상도 패턴(`\d+[xX×]\d+`) |

content_tag 탐색: `_get_content_tag(soup)` 헬퍼 — `.gh-content` → `.post-content` → `article` → `main` 순.
URL 정규화: `_clean_img_url(url)` — `_strip_ref_param()` + `clean_url()` 조합. Ghost CMS `ref` 쿼리 파라미터 제거.
Google Drive 링크 중 `/spreadsheets/`, `/forms/` 경로와 `_SKIP_LINK_HOSTS` 도메인은 건너뜀.
`_detect_non_image_urls(soup, post_url)` — 앵커 주변 heading에서 BGM/OST 등 비이미지 키워드 감지 시 해당 URL 제외.

---

## 다운로드 폴백 체인

### 기본 (모든 모드)

| 타입 | 1단계 | 2단계 | 3단계 |
|------|-------|-------|-------|
| `img` / `og_image` | 직접 + CT 검증 | Wayback `im_` | Wayback 포스트 스냅샷에서 img/og:image 탐색 |
| `gdrive` | 직접 (min 500B) | Wayback `im_` | Wayback 포스트 스냅샷에서 img 탐색 |
| `linked_keyword` | 직접 + CT 검증 (아카이브 허용) | Wayback `im_` (아카이브 허용) | Wayback 포스트 스냅샷에서 `<a>` 탐색 (아카이브 허용) |
| `linked_direct` | 직접 + CT 또는 확장자 (아카이브 허용) | community CDN에 한해 Wayback `im_` (아카이브 허용) | - |

### 확장 폴백 (retry 모드 전용)

기본 3단계에서 실패 시 아래 순서로 추가 시도. `linked_direct` 타입은 제외.

| 4단계 | 5단계 |
|-------|-------|
| **Kakao PF 폴백** | **다국어 Wayback 폴백** |

둘 다 성공 시 **파일 크기가 큰 쪽**이 primary, 작은 쪽이 alternative로 로그에 기록.

---

## Kakao PF 폴백 (retry 모드 전용)

### 상수

- `KAKAO_PF_PROFILE = "_YXZqxb"`
- `KAKAO_PF_API`: `https://pf.kakao.com/rocket-web/web/profiles/{PROFILE}/posts`
- `KAKAO_PF_INDEX_FILE = ROOT_DIR / "kakao_pf_index.json"` (캐시)
- `KAKAO_PF_LOG_FILE = IMAGES_DIR / "kakao_pf_log.tsv"` (성공 로그)

### KakaoPFPost

```python
class KakaoPFPost(NamedTuple):
    id: int
    title: str
    published_at: int    # Unix ms
    media_urls: list[str]
```

### 주요 함수

| 함수 | 설명 |
|------|------|
| `_build_kakao_pf_index() -> dict[str, list[KakaoPFPost]]` | API 페이지네이션으로 게시글 수집. JSON 캐시 파일이 있으면 로드 후 새 포스트만 추가 fetch. `{date: [KakaoPFPost, ...]}` 반환 |
| `_match_kakao_pf_post(candidates, blog_title) -> KakaoPFPost \| None` | 같은 날짜의 Kakao PF 후보 중 블로그 제목과 가장 유사한 포스트를 `difflib.SequenceMatcher`로 선택 |
| `_fetch_kakao_pf_image(post_url, img_url, post_date, utype, idx, kakao_pf_index, blog_title) -> tuple \| None` | 매칭된 Kakao PF 포스트에서 이미지 인덱스(idx) 기반으로 대체 이미지 추출 |

### 처리 흐름

1. `run_images`에서 `retry_mode=True` 시 `_build_kakao_pf_index()` 호출
2. 각 이미지의 `download_one_image`에서 기본 폴백 실패 후 `_fetch_kakao_pf_image()` 시도
3. `post_date` 기반으로 같은 날짜의 Kakao PF 게시글 탐색 → 제목 유사도 매칭
4. 매칭된 포스트의 `media_urls`에서 이미지 인덱스로 대체 이미지 선택
5. 성공 시 `_kakao_pf_log_buf`에 로그 기록

---

## 다국어 Wayback 폴백 (retry 모드 전용)

### 상수

- `MULTILANG_BLOG_HOSTS = {"en": "blog-en.lordofheroes.com", "ja": "blog-ja.lordofheroes.com"}`
- `MULTILANG_EARLIEST_DATE`: 언어별 최초 포스트 날짜
- `MULTILANG_LOG_FILE = IMAGES_DIR / "multilang_fallback.tsv"`

### 주요 함수

| 함수 | 설명 |
|------|------|
| `_build_multilang_date_index() -> dict[str, list[tuple[str, str]]]` | EN/JA 사이트맵에서 `{date: [(url, lang), ...]}` 인덱스 구축 |
| `_fetch_multilang_wayback_image(...)` | 대체 언어의 Wayback 포스트 스냅샷에서 같은 위치(인덱스) 기반으로 이미지 탐색 |

### 처리 흐름

1. `run_images`에서 `retry_mode=True` 시 EN/JA 사이트맵 인덱스 구축
2. 각 이미지의 `download_one_image`에서 기본 + Kakao PF 폴백 실패 후 시도
3. `post_date` 기반으로 같은 날짜의 EN/JA 포스트 URL 탐색
4. Wayback Machine에서 해당 포스트 스냅샷 fetch → 같은 인덱스 위치의 이미지 추출
5. 성공 시 `_multilang_log_buf`에 로그 기록

---

## 처리 흐름

`process_post()`에서 `extract_category(soup)` 호출 → `images/{category}/{YYYY}/{MM}/`에 저장 (카테고리 없으면 `images/etc/{YYYY}/{MM}/`). 일반 이미지·썸네일 모두 SHA-256 해시 기반 중복 체크 (`img_hashes: dict[str, str]`). 해시 중복 시 저장 생략, `image_map`에 기존 경로 매핑. 이미지는 최초 저장 위치에서 이동하지 않는다.

---

## Wayback 캐시

- `_wayback_oldest(url)`: CDX API `limit=5`, `fl=timestamp,original,statuscode`. 2xx/3xx 응답만 사용. `_strip_ref_param()` 적용 후 조회. `_wayback_cache`에 캐시. 동일 URL 동시 요청 시 `_wayback_events`로 선착 스레드만 fetch, 나머지는 대기 후 캐시 사용.
- `_add_im(url)`: `/web/{ts}/` → `/web/{ts}im_/`.
- `_fetch_wayback_post_soup`: 파싱 전 `resp.encoding = resp.apparent_encoding or "utf-8"`로 인코딩 보정 (Wayback 래퍼 페이지의 부정확한 헤더 대응).
- `_fetch_wayback_gdrive_from_post`: img 탐색 시 `src` 및 `data-src` 모두 확인. lazy-load 이미지가 Wayback 스냅샷에 `data-src`로만 남아있는 경우를 처리한다.
- Wayback 포스트 스냅샷 탐색 함수들은 `_normalized_link_key` 완전 일치 비교.

---

## 해시 캐시

`image_hashes.tsv` (형식: `sha256\trel_path\tT/빈값`). 모든 이미지(일반+썸네일) 통합 관리. 레거시 `thumbnail_hashes.txt`에서 자동 마이그레이션 (첫 실행 시). `_load_or_build_img_hashes()` → `(img_hashes, thumb_hashes)` 반환.

---

## run_images 시그니처

```python
def run_images(
    posts: list[tuple[str, str]],
    retry_mode: bool = False,
    force_download: bool = False,
    html_index: dict[str, Path] | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> None:
```

- `force_download=True`: `done_post_urls` 빈 dict으로 초기화, `seen_urls` 빈 set으로 초기화.
- `html_index`: `fetch_post_html(url, html_index)`를 통해 로컬 HTML 우선 조회.
- `retry_mode=True`: 다국어 Wayback 폴백 + Kakao PF 폴백 자동 활성화.

---

## 기타

- `save_image(content, filename, folder) -> str`: 충돌 시 `_2`, `_3` 번호 부여. 항상 유효한 파일명 반환.
- `failed_images.txt` 형식: `post_url\timg_url\treason`. fetch_post_failed는 img_url 빈 문자열.
- retry 모드: `fetch_post_failed`는 포스트 fetch 성공 시 제거. 개별 이미지 성공 시 `remove_from_failed_batch()`로 해당 img_url 엔트리만 일괄 제거. retry 대상 0개이면 메시지 출력 후 즉시 반환.
- `done_post_urls`: `dict[str, int]` (URL → 이미지 수). retry 모드에서는 이미지 수가 변경된 포스트만 재처리.
- `--backfill-map`: 기존 다운로드 이력으로 `image_map.tsv` 재구성. thumbnails 폴더는 `relative_to(IMAGES_DIR).parts`에 `"thumbnails"` 포함 여부로 제외.
- `image_map.tsv`에 썸네일(`og_image`) 경로도 기록한다.
- 단독 실행 시 `--posts` 기본값은 `all_links.txt`.
