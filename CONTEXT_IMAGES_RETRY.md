# 이미지 재처리 / 폴백

`--retry`, `--retry-fallback` 모드 및 관련 기능.

---

## --retry 재처리

1. `failed_images.txt`에서 고유 포스트 URL 추출 → 각 포스트의 모든 이미지 재처리
2. 이전 성공 이미지는 `seen_urls` 히트 → `"already"`(ok) → `remove_from_failed_batch()`로 해당 failed 엔트리 제거
3. 모든 이미지 ok(fail=0)이면 `done_post_urls`에 추가
4. **저장=0이어도** `"already"` 처리로 failed 엔트리가 줄어들어 다음 --retry 대상 감소

### 이미지 오버라이드

`config.toml`의 `[image_overrides]`로 영구 깨진 URL → 블로그 URL 수동 매핑. `--retry` 실행 시 `download_one_image()` 진입부에서 체크, `image_map`에 기록 + `failed_images`에서 제거. 다음 `html_local` 실행 시 stale 추적으로 자동 반영.

---

## --retry-fallback 확장 폴백

기본 다운로드와 완전 분리된 별도 파이프라인. `images_fallback/`에 저장. primary 트래킹 미수정 → MD/HTML에 영향 없음.

### 실패 이미지 필터링

`failed_images.txt`에서 포스트 단위가 아닌 **이미지 단위**로 필터링. `load_failed_image_entries()` → `{post_url: set[clean_img_url] | None}`. `None`은 `fetch_post_failed`(전체 재시도). 이미 성공한 이미지는 스킵.

### Kakao PF 폴백

Kakao PF API(`pf.kakao.com`, 프로필 `_YXZqxb`)에서 인증 없이 게시글 조회. JSON 캐시(`kakao_pf_index.json`)로 증분 갱신.

매칭: `published_time[:10]`으로 같은 날짜의 Kakao PF 게시글 탐색 → `difflib.SequenceMatcher`로 제목 유사도 매칭(임계값: `config.toml`의 `title_similarity_threshold`, 기본 0.55). 기각 시 로그 출력. published_time 없으면 `post_date`(lastmod) 폴백.

### 다국어 Wayback 폴백

EN/JA `all_links_{lang}.txt`에서 published_date 기반 인덱스 구축. `--retry-fallback` 실행 시에만 EN/JA HTML 다운로드·인덱스 구축이 수행된다. 캐시: `multilang_published_index.json` (all_links + html_dir mtime 비교로 갱신 판단).

#### 후보 스코어링

| 시그널 | 효과 |
|--------|------|
| 카테고리 일치 (`KO_TO_LANG_CAT` 매핑) | 주력 (단독 확정 ~40%) |
| lastmod 일치 | 보조 (~3% 추가) |

score > 0 → confirmed, score == 0 → unconfirmed.

#### 3단계 탐색

- **Phase A**: 원본 이미지 URL의 언어별 변환을 Wayback에서 직접 탐색.
- **Phase A-2**: **전체 후보** 순회. Wayback 포스트 스냅샷에서 URL 매칭 (안전하므로 unconfirmed 포함).
- **Phase B**: **confirmed만** 순회. 위치 기반 매칭. `content_img_idx`(KO 포스트 content `<img>` 태그 내 DOM 순서)를 사용하여 `collect_image_urls`의 raw idx와의 불일치를 보정. `og_image`/`<a>` 태그 수집분에 의한 인덱스 오프셋 문제를 해결. `content_img_idx`가 `None`(비 `<img>` 소스)이면 Phase B 스킵.

#### 결과 선택

Kakao + multilang 동시 시도. 둘 다 성공 시 **파일 크기가 큰 쪽**이 primary, 작은 쪽은 alt로 별도 저장.

### 폴백 로그

| 파일 | 역할 |
|------|------|
| `fallback_downloaded_urls.txt` | seen_key 트래킹 (재실행 시 중복 방지) |
| `fallback_image_map.tsv` | 원본 URL → 저장 경로 매핑 |
| `fallback_image_hashes.tsv` | SHA256 → 저장 경로 (해시 중복 제거) |
| `fallback_multilang.tsv` | multilang 소스 추적 (마지막 컬럼: phase A/A2/B) |
| `fallback_kakao_pf.tsv` | kakao 소스 추적 (마지막 컬럼: kakao) |
| `fallback_report.csv` | 성공 건 요약 리포트 |
| `fallback_still_failed.tsv` | fallback 후에도 남은 실패 목록 |

### 잔여 실패 리포트

`_generate_still_failed_report()`: `failed_images.txt` 전체에서 `fallback_multilang.tsv` + `fallback_kakao_pf.tsv`의 성공분을 빼고 `fallback_still_failed.tsv`에 기록. 폴백 완료 후 자동 생성.
