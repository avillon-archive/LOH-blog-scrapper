# download_images/ 패키지

15개 모듈로 분리된 패키지. 주요 진입점: `run_images`, `run_fallback_images`, `backfill_image_map`.

## 단독 실행

```bash
python -m download_images                    # 전체 이미지 수집
python -m download_images --retry            # 실패 재처리
python -m download_images --retry-fallback   # multilang/kakao 폴백
```

---

## 락 구조

- `_state_lock`: `seen_urls` / `img_hashes` / `image_map` / `thumb_hashes` in-memory 갱신.
- `_save_lock`: `save_image()` 파일명 충돌 해소 (디스크 I/O 직렬화).
- `_dl_lock`: `ImageFailedLog` 내부 캐시 + 파일 기록 원자성.

---

## 이미지 수집 (`collect_image_urls`)

URL 정규화: `clean_url()` (utils.py)이 모든 정규화를 담당. Ghost CMS `?ref=` 파라미터 제거, `/size/wNhN` 리사이즈 경로 제거(`w600`, `w256h256` 등), clovergames CDN `/p/`→`/o/` 변환(프리뷰→원본), trailing slash 제거. `_clean_img_url(url)`은 `clean_url()`의 얇은 래퍼.

`<img>` 수집 시 제외: `author-profile-image`, `kg-bookmark-icon` 클래스, `author-card` 내부. 수집 대상 호스트: `BLOG_HOST`(`/content/images/` 경로) 및 `storage.ghost.io`(Ghost 플랫폼 스토리지, 동일 `/content/images/` 경로), `COMMUNITY_CDN_HOST`(`community-ko-cdn.lordofheroes.com`), `COMMUNITY_SITE_HOST`(`community-ko.lordofheroes.com`), `GAME_CDN_HOST`(`cdn.clovergames.io`). Google Drive/이미지 호스트는 `is_gdrive_host()` (config.py)로 판별 — `gdrive_hosts` set 매칭 + `*.googleusercontent.com` 자동 인식(lh3~lh6 등 번호 무관). `/spreadsheets/`, `/forms/` 경로와 `_SKIP_LINK_HOSTS` 도메인은 건너뜀.

> **호스트 변경 (2026-04):** Ghost가 이미지 호스팅을 `blog-ko.lordofheroes.com/content/images/` → `storage.ghost.io/.../content/images/`로 변경. 기존 포스트는 예전 URL을 유지하지만, 이후 작성된 글은 새 호스트를 사용. 두 호스트 모두 동일한 `/content/images/` 경로 패턴으로 수집. 컨텐츠 해시 중복 처리로 동일 이미지의 재다운로드 방지.

`_detect_non_image_urls(soup, post_url)`: GDrive 앵커 주변에서 BGM/OST 등 비이미지 키워드 감지 시 제외. 검사 범위: ①앵커 텍스트, ②같은 부모 블록 내 이전 sibling(200자), ③content_tag **내부**의 이전 heading. content_tag 외부 heading은 무시.

---

## 다운로드 폴백 체인

### 기본 (모든 모드)

| 타입 | 1단계 | 2단계 | 3단계 |
|------|-------|-------|-------|
| `img` / `og_image` | 직접 + CT 검증 | Wayback `im_` | Wayback 포스트 스냅샷에서 img/og:image 탐색 |
| `gdrive` | 직접 (min 500B) | Wayback `im_` | Wayback 포스트 스냅샷에서 img 탐색 |
| `linked_keyword` | 직접 + CT 검증 | Wayback `im_` | Wayback 포스트 스냅샷에서 `<a>` 탐색 |
| `linked_direct` | 직접 + CT 또는 확장자 | community CDN만 Wayback `im_` | - |

> `linked_keyword`·`linked_direct`는 모든 단계에서 압축 파일(zip, rar, 7z 등)도 허용한다.

### 확장 폴백 (`--retry-fallback` 전용)

기본 다운로드와 완전 분리된 별도 파이프라인. 실패 이미지에 대해 Kakao PF + 다국어 Wayback을 **동시** 시도. 둘 다 성공 시 **파일 크기가 큰 쪽**이 primary. `images_fallback/`에 저장. primary 트래킹 미수정 → MD/HTML에 영향 없음.

---

## Wayback 캐시

- `_wayback_oldest(url)`: CDX API 조회. 입력 URL은 `collect` 단계에서 이미 정규화됨. 동일 URL 동시 요청 시 `_wayback_events`로 선착 스레드만 fetch, 나머지 대기 후 캐시 사용. `community-ko-cdn` URL이 CDX에 없으면 구 포럼 경로(`community-ko.lordofheroes.com/storage/app/public/media/...`)로 자동 재조회.
- `_fetch_wayback_post_soup`: `resp.encoding = resp.apparent_encoding or "utf-8"`로 인코딩 보정 (Wayback 래퍼의 부정확한 헤더 대응).
- `_fetch_wayback_gdrive_from_post`: `src` 및 `data-src` 모두 확인 (lazy-load 이미지가 Wayback 스냅샷에 `data-src`로만 남는 경우).

---

## 해시 중복 처리

SHA-256 해시 기반. 해시 중복 시 저장 생략, `image_map`에 기존 경로 매핑. 이미지는 최초 저장 위치에서 이동하지 않는다. 레거시 `thumbnail_hashes.txt (레거시)`에서 `image_hashes.csv`로 자동 마이그레이션.

---

## 이미지 오버라이드 (`[image_overrides]`)

`config.toml`의 `[image_overrides]` 섹션으로 영구 깨진 URL을 기존 이미지에 수동 매핑. `download_one_image()` 진입 시 체크하여 `image_map`에 기록 + `failed_images.csv`에서 제거. 다음 `html_local` 실행 시 stale 추적으로 자동 반영.

```toml
[image_overrides]
"https://lh3.googleusercontent.com/broken..." = "https://blog-ko.lordofheroes.com/content/images/..."
```

---

## 안전 중단

`run_images()`, `run_fallback_images()` 모두 `as_completed` 루프에서 `shutdown_event` 체크. 중단 시 대기열 `cancel()` 후 `break`, 기존 flush 코드(`_done_buf.flush_all()` 등)는 정상 실행된다.

---

## 콘솔 출력

```
[  100/2413 |  4.1% | Elapsed 00:01:23] 저장=50 중복=30 기존=5 실패=2
```

| 항목 | 의미 |
|------|------|
| **저장** | 디스크에 새 파일 저장 (`"original"` 반환) |
| **중복** | URL은 다르지만 SHA-256 동일, 저장 생략 (`"dup"`) |
| **기존** | `seen_urls` 히트, 이미 처리된 URL의 재등장 (`"already"`) |
| **오버라이드** | `[image_overrides]` 매핑으로 해소 (`"override"`) |
| **실패** | 다운로드 실패 (`""`) |

> `--retry`, `--retry-fallback` 관련 상세는 [CONTEXT_IMAGES_RETRY.md](CONTEXT_IMAGES_RETRY.md) 참조.
