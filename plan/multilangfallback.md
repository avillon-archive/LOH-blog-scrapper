# Multi-Language Wayback Fallback for Image Retry

## Context
블로그 이미지 다운로드 실패 시, `blog-en`/`blog-ja` 블로그의 Wayback Machine 스냅샷에서 동일 이미지를 탐색하는 폴백 단계를 추가한다. `--images --retry` 조합에서만 활성화된다.

배경:
- 이미지 파일명에서 `_KO`/`_EN`/`_JP` 접미사가 파일명 중간에도 나타남 (예: `Hero_KO_Banner.png`)
- EN/JA 블로그는 community CDN 대신 블로그 호스트(`blog-en`/`blog-ja`)를 사용
- 오래된 포스트(2020-2021)는 슬러그가 언어별로 완전히 다를 수 있음 → date 기반 폴백 필요
- 매칭 전략: URL/파일명 기반 → 실패 시 position(순서) 기반 폴백

## 수정 대상 파일
- [download_images.py](download_images.py) — 모든 변경이 이 파일에 집중

## 구현 계획

### 1. 상수 추가 (line ~48-51 부근)
```python
MULTILANG_BLOG_HOSTS = {
    "en": "blog-en.lordofheroes.com",
    "ja": "blog-ja.lordofheroes.com",
}
# 각 언어 블로그의 가장 오래된 포스트 날짜 — 이보다 이전 포스트는 폴백 불필요
MULTILANG_EARLIEST_DATE = {"en": "2020-10-20", "ja": "2021-01-15"}

# _KO suffix → target language suffix 매핑
# 파일명 어디에나 나타남: image_KO.png, Hero_KO_Banner.png 등
_KO_SUFFIX_RE = re.compile(r'(?i)_ko(?=[\._\-])')
_LANG_SUFFIX_MAP = {"en": "_EN", "ja": "_JP"}
```

### 2. 이미지 URL 후보 생성: `_multilang_image_url_candidates(img_url)`
- `blog-ko` 호스트 → `blog-en`/`blog-ja`로 교체
- `community-ko-cdn` 호스트 → `blog-en`/`blog-ja`로 교체 (EN/JA는 CDN 대신 블로그 호스트 사용)
- 파일명에서 `_KO`/`_Ko`/`_ko` → `_EN`/`_JP` 치환 (파일명 중간 포함, 모두 치환)
- KO 접미사가 없으면 호스트만 교체한 URL 반환
- 반환: `list[tuple[str, str]]` — `(candidate_url, lang)`

### 3. 다국어 사이트맵 인덱스 구축: `_build_multilang_date_index()`
- EN/JA 각각 Wayback CDX API로 `sitemap-posts.xml` 스냅샷 조회
- 스냅샷 fetch → XML 파싱 → `{date: [(post_url, lang), ...]}` dict 구축
- `build_posts_list.py`의 `parse_sitemap()` 재사용
- retry 시작 전 1회만 실행, read-only로 스레드에 전달
- 사이트맵 fetch 실패 시 해당 언어만 스킵

### 4. 포스트 URL 후보 생성: `_multilang_post_url_candidates(ko_post_url, post_date, date_index)`
- **1순위**: slug 기반 — `blog-ko` → `blog-en`/`blog-ja` 단순 치환
- **2순위**: date 기반 — `date_index[post_date]`에서 같은 날짜 포스트 검색
- 반환: `list[tuple[str, str]]` — `(candidate_post_url, lang)` (중복 제거)

### 5. Position 기반 이미지 탐색: `_fetch_wayback_img_by_position(...)`
```python
def _fetch_wayback_img_by_position(
    alt_post_url: str,
    idx: int,           # KO 포스트에서의 이미지 인덱스 (1-based)
    utype: str,
    post_soup_cache: dict,
) -> tuple[bytes, str, str, str] | None:
```
- Wayback에서 EN/JA 포스트 HTML 스냅샷 fetch (`_fetch_wayback_post_soup()` 재사용)
- `collect_image_urls(soup, alt_post_url)` 호출하여 이미지 목록 수집
- `idx` 위치의 이미지를 같은 utype으로 필터링 후 다운로드 시도
- 이미지 배치 순서가 언어별로 동일하다는 가정에 기반

### 6. 통합 폴백 함수: `_fetch_multilang_wayback_image(...)`
```
입력: post_url, img_url, post_date, utype, idx, date_index, post_soup_cache
```
- **조기 종료**: `post_date`가 모든 언어의 `MULTILANG_EARLIEST_DATE`보다 이전이면 즉시 `None` 반환
- 각 언어는 `post_date >= MULTILANG_EARLIEST_DATE[lang]`인 경우에만 시도

탐색 순서 (EN 먼저, 그 다음 JA):

**Phase A — URL/파일명 기반 매칭:**
1. `_multilang_image_url_candidates(img_url)`로 이미지 URL 후보 생성
2. 각 후보에 대해 `_fetch_wayback_image(candidate_img_url)` 직접 CDX 조회
3. 실패 시, `_multilang_post_url_candidates()`로 포스트 URL 후보 생성
4. 각 포스트 후보의 Wayback HTML에서 언어별 변환된 img_url로 매칭
   - `_fetch_wayback_img_from_post()` / `_fetch_wayback_gdrive_from_post()` / `_fetch_wayback_linked_from_post()` 재사용

**Phase B — Position 기반 매칭 (Phase A 전체 실패 시):**
5. 포스트 URL 후보에 대해 `_fetch_wayback_img_by_position(alt_post_url, idx, utype)` 호출
6. 첫 성공 시 즉시 반환

### 7. `download_one_image()` 수정 (line 865)
- 파라미터 추가: `post_date`, `multilang_fallback`, `multilang_date_index`
- 기존 모든 KO 폴백 실패 후(`payload is None`), `record_failed` 직전에 삽입:
```python
if payload is None and multilang_fallback and utype != "linked_direct":
    payload = _fetch_multilang_wayback_image(
        post_url, img_url, post_date, utype, idx,
        multilang_date_index or {}, post_soup_cache,
    )
```

### 8. `process_post()` 수정 (line 973)
- 파라미터 추가: `multilang_fallback`, `multilang_date_index`
- `download_one_image()` 호출 시 `post_date`와 함께 전달

### 9. `run_images()` 수정 (line 1154)
- `retry_mode=True`일 때만 `_build_multilang_date_index()` 호출
- `process_post()` submit 시 `multilang_fallback=True`, `multilang_date_index` 전달
- 인덱스 구축 결과 로그 출력

## 스레드 안전성
- `multilang_date_index`는 사전 구축 후 read-only → 락 불필요
- `_wayback_cache`/`_wayback_events`의 기존 이벤트 기반 동시성 제어가 새 URL에도 자동 적용
- `post_soup_cache`는 포스트당 1개 스레드 → 안전 (EN/JA 포스트 soup도 같은 캐시에 저장)

## run_all.py 변경 없음
- `retry_mode`는 이미 `run_images()`에 전달됨
- 폴백은 `run_images()` 내부에서 `retry_mode` 기반 게이팅

## 재사용하는 기존 함수
- `_wayback_oldest()` (line 473) — CDX API 조회
- `_fetch_wayback_image()` (line 595) — Wayback 이미지 직접 다운로드
- `_fetch_wayback_post_soup()` (line 611) — Wayback 포스트 HTML 파싱 + 캐싱
- `_fetch_wayback_img_from_post()` (line 672) — 포스트 HTML에서 img URL 매칭
- `_fetch_wayback_gdrive_from_post()` (line 637) — 포스트 HTML에서 gdrive 매칭
- `_fetch_wayback_linked_from_post()` (line 709) — 포스트 HTML에서 링크 매칭
- `collect_image_urls()` (line 741) — 포스트 HTML에서 이미지 URL 수집
- `build_posts_list.parse_sitemap()` — 사이트맵 XML 파싱

## 검증 방법
```bash
# 실패 이미지가 있는 상태에서 실행
python run_all.py --images --retry --sample 5
```
- 콘솔에 "다국어 Wayback 폴백 활성화" 메시지 확인
- EN/JA 사이트맵 인덱스 구축 로그 확인
- 기존 `--images` 단독 실행 시 폴백이 동작하지 않는 것 확인
