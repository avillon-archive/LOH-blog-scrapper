# 로드 오브 히어로즈 블로그 스크래퍼

`blog-ko.lordofheroes.com` 전체 포스트(사이트맵 기준 약 2,200개)의 이미지·MD·HTML을 로컬에 저장하는 Python 스크래퍼.

> 모듈별 상세 문서: [CONTEXT_UTILS.md](CONTEXT_UTILS.md) · [CONTEXT_HTML.md](CONTEXT_HTML.md) · [CONTEXT_IMAGES.md](CONTEXT_IMAGES.md) · [CONTEXT_MD.md](CONTEXT_MD.md) · [CONTEXT_RUN.md](CONTEXT_RUN.md)

---

## 인코딩 설정

모든 파일: **UTF-8 (BOM 없음, LF 줄 끝)**. `.editorconfig`가 강제 적용.

---

## 파일 구조

| 파일 | 역할 |
|------|------|
| `utils.py` | 공통 유틸 (세션, 재시도, 파일 I/O, rate limiter, `url_to_slug`, `VALID_CATEGORIES`, `extract_category`, `FailedLog`, `write_text_unique`, `run_pipeline`, `LineBuffer`, `build_html_index`, `fetch_post_html`) |
| `download_images.py` | 이미지 다운로드 (`ImageFailedLog` 클래스로 실패 이력 관리, 다국어·Kakao PF 폴백 포함) |
| `download_md.py` | HTML → MD 변환·저장 |
| `download_html.py` | 원문 HTML 저장 |
| `run_all.py` | 마스터 실행 스크립트 (실행 전 사이트맵 자동 갱신 포함) |
| `build_posts_list.py` | 사이트맵 파싱 → `loh_blog/all_posts.txt` / `all_pages.txt` / `all_links.txt` 생성 |
| `loh_blog/all_posts.txt` | sitemap-posts.xml URL+날짜 (`URL\tYYYY-MM-DD`, 날짜 **내림차순**) |
| `loh_blog/all_pages.txt` | sitemap-pages.xml URL+날짜 (`URL\tYYYY-MM-DD`, 날짜 **내림차순**) |
| `loh_blog/all_links.txt` | `all_posts.txt` + `all_pages.txt` 병합·중복 제거 목록 (기본 소스) |
| `loh_blog/custom_posts.txt` | 수동 작성 URL 목록. `all_posts.txt`와 동일한 포맷. `--custom` 옵션 사용 시 소스로 읽힘 |
| `requirements.txt` | `requests`, `beautifulsoup4`, `lxml` |

> **모듈 독립성**: `download_html.py`와 `download_md.py`는 서로 의존하지 않는다. `url_to_slug`는 `utils.py`에 정의되어 있으며 두 모듈이 공통 import한다.

---

## 실행 방법

```bash
pip install requests beautifulsoup4 lxml

python3 run_all.py                      # images + md + html 전체 (all_links.txt 자동 갱신)
python3 run_all.py --images             # 이미지만 (html 항상 포함)
python3 run_all.py --md                 # MD만 (html 항상 포함)
python3 run_all.py --html               # HTML만
python3 run_all.py --retry              # 실패 목록 재처리
python3 run_all.py --sample 10          # 랜덤 10개 테스트 (all_links.txt 행 수의 10% 상한)
python3 run_all.py --sample 10 --retry  # 실패 목록에서 10개
python3 run_all.py --posts              # all_posts.txt 소스 사용 (해당 사이트맵 개별 갱신 체크)
python3 run_all.py --posts --md         # all_posts.txt 대상 MD만
python3 run_all.py --pages              # all_pages.txt 소스 사용 (해당 사이트맵 개별 갱신 체크)
python3 run_all.py --pages --images     # all_pages.txt 대상 이미지만
python3 run_all.py --custom             # custom_posts.txt 소스 사용 (사이트맵 갱신 건너뜀, 강제 재다운로드)
python3 run_all.py --custom --md        # custom_posts.txt 대상 MD만

python3 build_posts_list.py             # all_posts.txt / all_pages.txt / all_links.txt 수동 재생성
```

파이프라인 실행 순서: `html → images → md` 고정. HTML을 먼저 실행하여 로컬 캐시를 구축하고, images/md 단계에서 재활용한다. `--images`나 `--md`만 지정해도 HTML 단계는 항상 포함된다.

**옵션 제약**: `--posts`, `--pages`, `--custom`은 상호 배타적이며 동시에 사용할 수 없다. 세 플래그 모두 `--sample`과 동시에 사용할 수 없다.

---

## 저장 구조

```
./loh_blog/
  all_posts.txt              ← sitemap-posts.xml URL+날짜 목록 (날짜 내림차순)
  all_pages.txt              ← sitemap-pages.xml URL+날짜 목록 (날짜 내림차순)
  all_links.txt              ← all_posts.txt + all_pages.txt 병합·중복 제거 (기본 소스)
  custom_posts.txt           ← 수동 작성 URL 목록 (--custom 옵션 소스)
  kakao_pf_index.json        ← Kakao PF 게시글 캐시 (API 응답 JSON)
  images/카테고리명/YYYY/MM/  ← 본문 이미지 (카테고리·날짜별 폴더, 고유 이미지)
  images/카테고리명/          ← 재사용 일반 이미지 (해시 중복 2+회 출현)
  images/카테고리명/thumbnails/ ← 재사용 썸네일 (해시 중복 2+회 출현)
  images/etc/YYYY/MM/        ← 카테고리 없는 본문 이미지 (고유)
  images/common/             ← 카테고리 없는 재사용 일반 이미지
  images/common/thumbnails/  ← 카테고리 없는 재사용 썸네일
  images/multilang_fallback.tsv ← 다국어 Wayback 폴백 성공 로그
  images/kakao_pf_log.tsv    ← Kakao PF 폴백 성공 로그
  md/                        ← 카테고리 없는 MD 파일
  md/카테고리명/              ← 카테고리별 MD 파일
  html/                      ← 카테고리 없는 원문 HTML
  html/카테고리명/            ← 카테고리별 원문 HTML
  downloaded_urls.txt        ← 이미지 URL 완료 이력 (main:/thumb: prefix)
  done_posts_images.txt      ← 이미지 완료 포스트 URL 목록
  image_map.tsv              ← clean_url → images/... 상대경로 (ROOT_DIR 기준)
  thumbnail_hashes.txt       ← 썸네일 SHA-256 해시 캐시 (레거시, 마이그레이션용)
  image_hashes.tsv           ← 통합 이미지 해시 캐시 (hash\trel_path\tT/빈값)
  done_md.txt                ← MD 완료 이력 (slug\tpost_url)
  done_html.txt              ← HTML 완료 이력 (slug\tpost_url)
  failed_images.txt          ← 이미지 실패 이력 (post_url\timg_url\treason)
  failed_md.txt              ← MD 실패 이력 (post_url\treason)
  failed_html.txt            ← HTML 실패 이력 (post_url\treason)
```

MD 파일 내 이미지 참조는 MD 파일 위치 기준 상대경로. `img_prefix`는 `process_post`에서 `target_dir`의 ROOT_DIR 기준 depth로 자동 계산된다.

| MD 파일 위치 | depth | img_prefix | 실제 경로 |
|---|---|---|---|
| `md/slug.md` | 1 | `../` | `../images/etc/YYYY/MM/x.png` |
| `md/카테고리/slug.md` | 2 | `../../` | `../../images/카테고리/YYYY/MM/x.png` |

`image_map.tsv`에 없는 이미지는 절대 URL로 폴백. 썸네일(`og_image`)도 `image_map.tsv`에 기록한다.

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

## 네트워크 설정

- 블로그 도메인 요청은 토큰 버킷 rate limiter로 속도 제한 (대규모 배치: 10 req/s, 소규모 배치 ≤100건: 20 req/s)
- HTTP 429 Retry-After 헤더를 존중하며 retry 횟수를 소모하지 않는다
- Claude 컨테이너 환경은 네트워크 활성화 상태. 단, 허용된 도메인 목록(`api.anthropic.com`, `github.com`, `pypi.org` 등)만 접근 가능하며, `blog-ko.lordofheroes.com` 및 `web.archive.org`는 허용 목록에 포함되지 않는다. 의존성 설치(`pip install`)는 컨테이너 내에서 직접 수행 가능하고, 실제 스크래핑은 허용 도메인이 포함된 로컬 환경에서 수행한다.
