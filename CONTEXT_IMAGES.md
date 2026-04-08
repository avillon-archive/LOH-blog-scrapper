# download_images/ 패키지

15개 모듈로 분리된 패키지. 주요 진입점: `run_images`, `run_fallback_images`, `backfill_image_map`.

## 단독 실행

```bash
python -m download_images                    # 전체 이미지 수집
python -m download_images --retry            # 실패 재처리
python -m download_images --retry-fallback   # multilang/kakao 폴백
python -m download_images --backfill-map     # image_map.tsv 누락 보충
```

---

## 락 구조

- `_state_lock`: `seen_urls` / `img_hashes` / `image_map` / `thumb_hashes` in-memory 갱신.
- `_save_lock`: `save_image()` 파일명 충돌 해소 (디스크 I/O 직렬화).
- `_dl_lock`: `ImageFailedLog` 내부 캐시.

---

## 이미지 수집 (`collect_image_urls`)

URL 정규화: `_clean_img_url(url)` — `_strip_ref_param()` + `clean_url()` 조합. Ghost CMS `ref` 쿼리 파라미터와 `/size/wN` 리사이즈 경로를 수집 시점에서 제거하여 원본 해상도 다운로드.

`<img>` 수집 대상 호스트: `BLOG_HOST`(`/content/images/` 경로), `COMMUNITY_CDN_HOST`(`community-ko-cdn.lordofheroes.com`), `GAME_CDN_HOST`(`cdn.clovergames.io`). Google Drive 호스트는 `gdrive` 타입으로 별도 수집. `/spreadsheets/`, `/forms/` 경로와 `_SKIP_LINK_HOSTS` 도메인은 건너뜀.

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

## Kakao PF 폴백

Kakao PF API(`pf.kakao.com`, 프로필 `_YXZqxb`)에서 인증 없이 게시글 조회. JSON 캐시(`kakao_pf_index.json`)로 증분 갱신.

매칭: `published_time[:10]`으로 같은 날짜의 Kakao PF 게시글 탐색 → `difflib.SequenceMatcher`로 제목 유사도 매칭. published_time 없으면 `post_date`(lastmod) 폴백.

---

## 다국어 Wayback 폴백 (`--retry-fallback` 전용)

EN/JA `all_links_{lang}.txt`에서 published_date 기반 인덱스 구축. `--retry-fallback` 실행 시에만 EN/JA HTML 다운로드·인덱스 구축이 수행된다. 캐시: `multilang_published_index.json` (all_links + html_dir mtime 비교로 갱신 판단).

### 후보 스코어링

| 시그널 | 효과 |
|--------|------|
| 카테고리 일치 (`KO_TO_LANG_CAT` 매핑) | 주력 (단독 확정 ~40%) |
| lastmod 일치 | 보조 (~3% 추가) |

score > 0 → confirmed, score == 0 → unconfirmed.

### 3단계 탐색

- **Phase A**: 원본 이미지 URL의 언어별 변환을 Wayback에서 직접 탐색.
- **Phase A-2**: **전체 후보** 순회. Wayback 포스트 스냅샷에서 URL 매칭 (안전하므로 unconfirmed 포함).
- **Phase B**: **confirmed만** 순회. 위치(idx) 기반 매칭 (오매칭 위험 → 시그널 일치 후보로 제한).

---

## Wayback 캐시

- `_wayback_oldest(url)`: CDX API, `_strip_ref_param()` 적용 후 조회. 동일 URL 동시 요청 시 `_wayback_events`로 선착 스레드만 fetch, 나머지 대기 후 캐시 사용.
- `_fetch_wayback_post_soup`: `resp.encoding = resp.apparent_encoding or "utf-8"`로 인코딩 보정 (Wayback 래퍼의 부정확한 헤더 대응).
- `_fetch_wayback_gdrive_from_post`: `src` 및 `data-src` 모두 확인 (lazy-load 이미지가 Wayback 스냅샷에 `data-src`로만 남는 경우).

---

## 해시 중복 처리

SHA-256 해시 기반. 해시 중복 시 저장 생략, `image_map`에 기존 경로 매핑. 이미지는 최초 저장 위치에서 이동하지 않는다. 레거시 `thumbnail_hashes.txt`에서 `image_hashes.tsv`로 자동 마이그레이션.

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
| **실패** | 다운로드 실패 (`""`) |

### --retry 재처리 메커니즘

1. `failed_images.txt`에서 고유 포스트 URL 추출 → 각 포스트의 모든 이미지 재처리
2. 이전 성공 이미지는 `seen_urls` 히트 → `"already"`(ok) → `remove_from_failed_batch()`로 해당 failed 엔트리 제거
3. 모든 이미지 ok(fail=0)이면 `done_post_urls`에 추가
4. **저장=0이어도** `"already"` 처리로 failed 엔트리가 줄어들어 다음 --retry 대상 감소
