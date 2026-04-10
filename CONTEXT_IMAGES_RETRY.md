# 이미지 재처리 / 폴백

`--retry`, `--retry-fallback` 모드 및 관련 기능.

---

## --retry 재처리

1. `failed_images.csv`에서 고유 포스트 URL 추출 → 각 포스트의 모든 이미지 재처리
2. 이전 성공 이미지는 `seen_urls` 히트 → `"already"`(ok) → `remove_from_failed_batch()`로 해당 failed 엔트리 제거
3. 모든 이미지 ok(fail=0)이면 `done_post_urls`에 추가
4. **저장=0이어도** `"already"` 처리로 failed 엔트리가 줄어들어 다음 --retry 대상 감소

### 알려진 엣지 케이스

파일 잠금 등으로 이미지는 디스크에 저장됐지만 로그(`image_map`, `downloaded_urls`, `image_hashes`)가 갱신되지 않은 경우, 재실행 시 다시 다운로드하여 복구된다. 단, 재실행 전에 **소스가 영구히 죽으면**(Wayback URL 삭제 등) 디스크에 파일은 있지만 트래킹에서 빠진 고아 파일이 된다. 파일명은 다운로드 응답에서 결정되므로 다운로드 없이 디스크 매칭은 불가. 또한 해시도 미기록이므로 파일 무결성 검증 수단이 없다. `requests`는 응답 전체 수신 후 `content`를 반환하므로 네트워크 중단 시 파일 저장까지 도달하지 않지만, 서버가 잘린 응답을 200 OK로 반환한 극단적 경우에는 손상 파일이 남을 수 있다. 발생 확률은 극히 낮으나, 의심 시 `images/` 디렉토리와 `image_map.csv`를 비교하여 고아 파일을 찾고, 이미지 뷰어로 수동 확인 가능.

### community CDN → 구 포럼 URL 자동 변환

`community-ko-cdn` URL이 Wayback CDX에 없으면, `_fetch_wayback_image`가 구 포럼 경로(`community-ko.lordofheroes.com/storage/app/public/media/...`)로 자동 재조회. 블로그 이전 전 포럼 시절의 이미지가 Wayback에 보존된 경우 복구 가능. `--images`, `--retry` 모두 적용.

### 이미지 오버라이드

`config.toml`의 `[image_overrides]`로 영구 깨진 URL → 블로그 URL 수동 매핑. `--retry` 실행 시 `download_one_image()` 진입부에서 체크, `image_map`에 기록 + `failed_images`에서 제거. target이 로컬에 없으면 target URL에서 직접 다운로드(Wayback 포함). 다운로드한 콘텐츠의 SHA256 해시가 `img_hashes`에 이미 있으면 기존 경로를 재사용하여 중복 저장을 방지한다. 다음 `html_local` 실행 시 stale 추적으로 자동 반영.

### heading 기반 폴백 (공지사항 전용)

`download_images/fallback_heading.py`. `--retry` 모드에서 `download_one_image()` 실패 후, 공지사항(`category == "공지사항"`) 포스트의 `img` 타입 이미지에 한해 자동 시도.

#### 동작 원리

공지사항 포스트는 동일 섹션(예: "아슬란과의 합동 훈련")이 날짜별로 반복 등장하며 같은 이미지를 사용한다. 깨진 이미지의 직전 `<h3>` heading을 추출하고, 다른 공지사항 포스트에서 유사한 heading을 가진 섹션을 찾아 위치 기반으로 이미지를 복구한다.

#### 처리 흐름

1. 깨진 이미지의 직전 `<h3>` 텍스트 및 섹션 내 위치(0-based) 추출 (`_find_heading_context`)
2. ±2개월 범위의 공지사항 포스트 중, `difflib.SequenceMatcher` 유사도 ≥ 0.90인 h3를 가진 도너 포스트를 날짜 근접순으로 검색 (`_find_donor_posts`)
3. 도너 섹션의 이미지 개수가 원본 섹션과 다르면 스킵 (이미지 개수 가드)
4. 같은 position의 도너 이미지 URL로 `_fetch_image()` → `_fetch_wayback_image()` 시도
5. 도너 이미지도 깨졌으면 다음 도너 시도

#### 제약

- **카테고리**: 공지사항만 대상 (h3 섹션 반복 패턴이 있는 카테고리)
- **utype**: `img`, `gdrive` (h3 섹션 아래 `<img>` 태그). `og_image`, `linked_*` 제외
- **유사도 임계값**: 0.90 (하드코딩). "Event 7. X" vs "Event 5. X" → 0.95 통과, 완전히 다른 키워드 → 0.50 거부
- **날짜 범위**: ±62일 (~2개월)
- **인덱스 미리 구축 없음**: 실패 이미지마다 lazy로 도너 검색 (±2개월 공지사항 HTML만 파싱)
- **카테고리 필터**: `html_index` path에 `공지사항/`이 포함되는지로 빠르게 판별 (HTML 파싱 전)
- `[image_overrides]`에 이미 매핑된 URL은 override가 먼저 적용되므로 heading 폴백까지 도달하지 않음

---

## --retry-fallback 확장 폴백

기본 다운로드와 완전 분리된 별도 파이프라인. `images_fallback/`에 저장. primary 트래킹 미수정 → MD/HTML에 영향 없음.

### 실패 이미지 필터링

`failed_images.csv`에서 포스트 단위가 아닌 **이미지 단위**로 필터링. `load_failed_image_entries()` → `{post_url: set[clean_img_url] | None}`. `None`은 `fetch_post_failed`(전체 재시도). 이미 성공한 이미지는 스킵.

### Kakao PF 폴백

Kakao PF API(`pf.kakao.com`, 프로필 `_YXZqxb`)에서 인증 없이 게시글 조회. JSON 캐시(`kakao_pf_index.json`)로 증분 갱신.

매칭: `published_time[:10]`으로 같은 날짜의 Kakao PF 게시글 탐색 → `difflib.SequenceMatcher`로 제목 유사도 매칭(임계값: `config.toml`의 `title_similarity_threshold`, 기본 0.55). 기각 시 로그 출력. published_time 없으면 `post_date`(lastmod) 폴백.

### 다국어 Wayback 폴백

EN/JA `all_links_{lang}.csv`에서 published_date 기반 인덱스 구축. `--retry-fallback` 실행 시에만 EN/JA HTML 다운로드·인덱스 구축이 수행된다. 캐시: `multilang_published_index.json` (all_links + html_dir mtime 비교로 갱신 판단).

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

폴백 로그 파일 인벤토리는 [CONTEXT_LOG.md](CONTEXT_LOG.md) 참조.

### 잔여 실패 리포트

`_generate_still_failed_report()`: `failed_images.csv` 전체에서 `fallback_multilang.csv` + `fallback_kakao_pf.csv`의 성공분을 빼고 `fallback_still_failed.csv`에 기록. 폴백 완료 후 자동 생성.
