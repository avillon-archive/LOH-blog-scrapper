# 로드 오브 히어로즈 블로그 스크래퍼

`blog-ko.lordofheroes.com` 전체 포스트(~2,200개)의 이미지·MD·HTML을 로컬에 저장하는 Python 스크래퍼.

> 모듈별 상세: [CONTEXT_UTILS.md](CONTEXT_UTILS.md) · [CONTEXT_IMAGES.md](CONTEXT_IMAGES.md) · [CONTEXT_RUN.md](CONTEXT_RUN.md) · [CONTEXT_HTML_LOCAL.md](CONTEXT_HTML_LOCAL.md)

---

## 파이프라인

의존 관계: **html → images → {md, html-local}**. md와 html-local은 상호 의존 없음 (둘 다 `image_map.tsv`를 참조).

실행 순서: `("html", "images", "md", "html-local")` 고정. html은 `--images`나 `--md`만 지정해도 항상 포함 (로컬 캐시 구축). html-local은 명시적 지정 또는 전체 실행 시에만 포함.

---

## 저장 구조

```
./loh_blog/
  all_links.txt              ← all_posts + all_pages 병합 (기본 소스, 날짜 내림차순)
  images/{카테고리}/YYYY/MM/  ← 본문 이미지
  images/etc/YYYY/MM/        ← 카테고리 없는 이미지
  images_fallback/            ← --retry-fallback 보존용 폴백 이미지
  md/[카테고리/]              ← Markdown
  html/[카테고리/]            ← KO 원문 HTML
  html_en/, html_ja/         ← EN/JA 원문 HTML (flat, 카테고리 없음)
  html_local/[카테고리/]     ← 오프라인 열람용 HTML
```

트래킹 파일: `done_*.txt`(완료), `failed_*.txt`(실패), `stale_*.txt`(재생성 대상), `downloaded_urls.txt`, `image_map.tsv`, `image_hashes.tsv`.

---

## 카테고리

`VALID_CATEGORIES` (10개): 공지사항, 이벤트, 갤러리, 유니버스, 아발론서고, 쿠폰, 아발론 이벤트, Special, 가이드, 확률 정보.

`extract_category(soup)`: `<meta property="article:tag">` 중 **첫 번째** 값이 유효 카테고리면 반환, 아니면 `""`. EN/JA HTML은 항상 `""` → flat 저장.

---

## download_html.py

- Content-Type `text/html` 검증, 실패 시 `unexpected_content_type:...` 기록.
- 모듈 레벨 락·FailedLog는 하위 호환용 래퍼. `run_html()` 내부에서 독립 인스턴스 생성.
- HTML 파이프라인은 `html_index`를 받지 않음 (자신이 원본을 생성하는 첫 단계).

---

## download_md.py

- `markitdown` 패키지 사용, 스레드별 인스턴스를 `_thread_local`로 캐싱.
- 제목 탐색: `h1.post-title` → `h1` → `og:title` 순.
- 본문 탐색: `section.post-content` → `div.post-content` → `article` → `main` 순.
- `_flatten_nested_inline(body)`: Ghost CMS의 중첩 inline 태그(`<strong><strong>...</strong></strong>`) 평탄화. markitdown에서 `**********text**********` 마커 누적 방지.
- `image_map.tsv` 기반 이미지 상대경로. `img_prefix`는 MD 파일의 ROOT_DIR 기준 depth로 계산 (`md/` → `"../"`, `md/카테고리/` → `"../../"`). 미등록 이미지는 절대 URL.

### stale 추적 (image_map 갱신 시 선택적 재생성)

`stale_md.txt`에 image_map에 없어서 절대 URL로 남은 이미지의 `clean_url`을 포스트별 기록 (`post_url\turl1|url2|...`). 다음 `run_md` 실행 시 stale 항목과 현재 image_map을 대조하여, 이제 매핑 가능해진 포스트만 자동 재생성. `backfill_stale.py`로 기존 파일에서 초기 구축.

### MD 헤더 형식

```
# 제목
**작성일:** YYYY-MM-DD
**카테고리:** 카테고리명  ← 없으면 이 줄 생략
**원문:** URL

---
```

작성일: `article:published_time` 우선, 없으면 포스트 목록의 날짜 폴백.
