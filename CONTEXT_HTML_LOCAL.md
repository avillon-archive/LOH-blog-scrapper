# download_html_local.py

블로그 운영 중단 대비, 다운로드된 KO HTML을 오프라인 열람 가능하도록 후처리. 원본 `html/`은 보존하고 `html_local/`에 별도 저장. KO 전용.

---

## 모듈 구조

| 파일 | 역할 |
|------|------|
| `asset_downloader.py` | `BaseAssetDownloader` + `CssDownloader` + `SiteImageDownloader` |
| `download_html_local.py` | `HtmlLocalizer` + `_process_post` + `run_html_local` (공개 API) |
| `listing_pages.py` | 카테고리 목록 페이지·홈 인덱스 생성 (`generate_listing_pages`) |

순환 방지: `listing_pages` → `download_html_local` 톱레벨 임포트, `download_html_local` → `listing_pages`는 `run_html_local()` 내 지연 임포트.

---

## 아키텍처

### BaseAssetDownloader (`asset_downloader.py`)

CssDownloader·SiteImageDownloader 공통 패턴:

- **per-URL 락**: 같은 URL의 동시 다운로드 방지 (전역 락이 아니라 URL별 Lock).
- **double-check-exists**: 락 획득 전후로 파일 존재 확인 → 중복 다운로드 방지.
- **파일명 해시**: `{stem}_{md5[:8]}.{ext}` 형식으로 충돌 방지.

서브클래스 오버라이드: `_default_name`, `_default_ext`, `_save(resp, local_path, url)`, `_should_download(url)`.

### CssDownloader

CSS 다운로드·캐싱. CSS 내 `url()` 상대경로는 절대 URL로 정규화.

### SiteImageDownloader

블로그 사이트 크롬 이미지(favicon, 네비 로고, GM 아바타 등)를 `assets/`에 다운로드. `image_map.tsv`에 없는 블로그 도메인(`blog-ko.lordofheroes.com/content/images/`) 이미지만 대상.

### HtmlLocalizer

포스트 1건당 인스턴스 생성. `localize()` 변환 순서:

1. **CSS 로컬화**: `<link rel="stylesheet">` → CssDownloader 로컬 경로
2. **이미지 리라이트**: `image_map.tsv` 기반. 매핑 실패 시 SiteImageDownloader 폴백 또는 절대 URL. `srcset`·`data-src` 제거. `<style>` bg-image, `<meta og:image>`, JSON-LD도 처리.
3. **내부 링크 로컬화**: `slug_map` 기반 파일 간 상대경로. `/tag/{slug}/` → `{category}/index.html`. 블로그 루트 → `index.html`.
4. **홈 로고 리라이트**: `<a class="site-nav-logo">` → `index.html`
5. **YouTube iframe 반응형**: `width`/`height` 속성 제거 → `style="width:100%; aspect-ratio:16/9;"` 적용.
6. **JS 제거**: 모든 `<script>` 제거, `<noscript>` 유지.

---

## 상대경로 산출

| 소스 위치 | images | assets | 타 카테고리 |
|-----------|--------|--------|-------------|
| `html_local/{slug}.html` | `../images/...` | `assets/...` | `{category}/B.html` |
| `html_local/{category}/{slug}.html` | `../../images/...` | `../assets/...` | `../{category}/B.html` |

---

## 카테고리 목록 페이지 (`listing_pages.py`)

`TAG_SLUG_TO_CATEGORY`: notices→공지사항, events→이벤트, gallery→갤러리, universe→유니버스, library→아발론서고, coupon→쿠폰.

`generate_listing_pages()` — `run_html_local()` 끝에서 자동 호출:

1. `html/` 전체에서 메타데이터 1회 수집 → 카테고리별 분류.
2. 각 태그 페이지의 **1페이지만** fetch → 레이아웃 템플릿 (캐시: `listing_cache/`).
3. 로컬 메타데이터로 post-card HTML 생성 → published_time 내림차순.
4. HtmlLocalizer 적용 후 `html_local/{category}/index.html` 저장.

### stale 추적

`stale_html_local.txt` — MD와 동일 패턴. `HtmlLocalizer`가 unmapped URL을 수집, `run_html_local` 실행 시 자동 refresh 판정.

**홈페이지** (`index_all.html`): `_find_prob_linked_slugs()`로 확률 정보 카테고리에서 2단계 링크 체인으로 도달 가능한 페이지(~71건)를 제외 (확률 정보 메뉴로 이미 접근 가능). 나머지 전부 포함.
