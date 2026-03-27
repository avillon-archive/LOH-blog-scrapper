# Plan: 이미지 파이프라인 공유 이미지 재배치 로직 제거 및 구조 개선

## Context

`download_images.py`의 "공유 이미지 재배치" 시스템(`_relocate_shared_images`)이 반복적으로 오동작을 일으키고 있다:
- 고유 이미지를 "공유 이미지"로 잘못 분류하여 카테고리 루트로 이동
- 재배치 후 `image_map.tsv`/`image_hashes.tsv`는 갱신되지만 `downloaded_urls.txt`/`done_posts_images.txt`는 갱신되지 않아 상태 불일치 발생
- 이동된 이미지의 날짜 컨텍스트(YYYY/MM) 소실
- 재실행 시 이전 상태와 충돌

사용자는 이전 실행 내역과의 backward 호환성을 포기하고, 클린 실행 후 오동작이 없는 구조로 리팩토링을 요청했다.

**목표**: 이미지는 최초 저장 위치(`images/{category}/YYYY/MM/`)에 영구 고정. 해시 중복 검사는 "동일 콘텐츠 재저장 방지"에만 사용하고, 파일 이동에는 절대 사용하지 않는다.

---

## Step 1: `hash_dup_count` 및 `_relocate_shared_images` 완전 제거

**파일**: [download_images.py](download_images.py)

### 1a. `_relocate_shared_images` 함수 삭제 (lines 2141-2236)
- `_CAT_DATE_RE` 정규식 (lines 2135-2138) 함께 삭제

### 1b. `hash_dup_count` 매개변수 제거
- `download_one_image` 시그니처에서 제거 (line 1824)
- `process_post` 시그니처에서 제거 (line 2031)
- `process_post` 내 `download_one_image` 호출에서 제거 (line 2095)

### 1c. `run_images`에서 관련 코드 제거
- `hash_dup_count: dict[str, int] = {}` 초기화 삭제 (line 2310)
- `executor.submit` 호출에서 `hash_dup_count` 인자 삭제 (line 2361)
- Phase 2 재배치 블록 삭제 (lines 2410-2414)

### 1d. `download_one_image` 내부에서 `hash_dup_count` 증가 코드 삭제
- line 1951의 `hash_dup_count[content_hash] = ...` 삭제
- 해시 중복 감지 자체(`existing_rel = img_hashes.get(content_hash)`)는 유지 — 동일 콘텐츠 재저장 방지에 필요

---

## Step 2: 레거시 `_thumb_buf` LineBuffer 삭제

**파일**: [download_images.py](download_images.py)

- line 113의 `_thumb_buf = LineBuffer(THUMB_HASH_FILE)` 삭제 (주석에 이미 "레거시, 마이그레이션 후 미사용"으로 표시)

---

## Step 3: `PostProcessResult`에 `ok_dedup` 필드 추가 및 로그 개선

**파일**: [download_images.py](download_images.py)

### 3a. 데이터클래스 확장
```python
@dataclass
class PostProcessResult:
    ok: int
    fail: int
    post_fetch_ok: bool
    ok_saved: int = 0
    ok_original: int = 0
    ok_multilang: int = 0
    ok_kakao: int = 0
    ok_dedup: int = 0          # 새로 추가: 해시 중복으로 저장 건너뜀
    succeeded_urls: list[str] = field(default_factory=list)
```

### 3b. `process_post`에서 `"dup"` 결과 집계
현재 `"dup"`은 `"already"`와 함께 그냥 `ok += 1`만 하고 있다. `"dup"` 일 때 `ok_dedup += 1` 추가.

### 3c. `run_images`에서 dedup 통계 집계 및 출력
- `total_dedup` 카운터 추가
- 진행도 및 최종 로그에 `중복건너뜀=N` 표시
- 기존 `기존=` (already) 와 구분하여 표시

---

## Step 4: `run_images` 로그 형식 개선

**파일**: [download_images.py](download_images.py)

### 현재 로그 (일반 모드)
```
  [done/total | % | Elapsed] 저장=X 기존=Y 실패=Z
```

### 개선 후 로그 (일반 모드)
```
  [done/total | % | Elapsed] 저장=X 중복=Y 기존=Z 실패=W
```

### 현재 로그 (retry 모드)
```
  [done/total | % | Elapsed] 저장=X (원본=A multilang=B kakao=C) 기존=Y 실패=Z
```

### 개선 후 로그 (retry 모드)
```
  [done/total | % | Elapsed] 저장=X (원본=A multilang=B kakao=C) 중복=Y 기존=Z 실패=W
```

- `중복=Y`: 해시 중복으로 저장 생략 (파일은 이미 존재, URL만 매핑 추가)
- `기존=Z`: 이미 다운로드된 URL (seen_urls 히트)
- 최종 완료 로그도 동일하게 수정

---

## Step 5: 문서 업데이트

### 5a. [CONTEXT_IMAGES.md](CONTEXT_IMAGES.md)
- line 22: `_state_lock` 설명에서 `hash_dup_count` 제거
- lines 161-168: "2단계 처리 흐름" 섹션에서 Phase 2 설명 제거, Phase 1만 유지
- line 200: `force_download` 설명에서 "Phase 2 재배치 건너뜀" 제거

### 5b. [CONTEXT.md](CONTEXT.md)
- lines 74-79: 저장 구조에서 카테고리 루트 / common / thumbnails 관련 항목 제거
- 모든 이미지는 `images/카테고리명/YYYY/MM/`에만 저장됨을 명시

---

## 수정 대상 파일 요약

| 파일 | 변경 내용 |
|------|-----------|
| `download_images.py` | `_relocate_shared_images` 삭제, `hash_dup_count` 완전 제거, `_thumb_buf` 삭제, `PostProcessResult.ok_dedup` 추가, 로그 형식 개선 |
| `CONTEXT_IMAGES.md` | Phase 2 문서 제거, 락 설명 업데이트 |
| `CONTEXT.md` | 저장 구조 업데이트 (카테고리 루트/common/thumbnails 제거) |

---

## 검증 방법

1. **정적 검증**: `hash_dup_count`, `_relocate_shared_images`, `_CAT_DATE_RE` 문자열이 코드에 남아있지 않은지 grep 확인
2. **구문 검증**: `python -c "import download_images"` — import 오류 없이 모듈 로드 확인
3. **Dry-run**: `python run_all.py --images --sample 5 --seed 42` — 소규모 샘플로 실행하여:
   - 이미지가 `images/{category}/YYYY/MM/`에만 저장되는지 확인
   - 카테고리 루트로 이동되는 파일이 없는지 확인
   - 로그에 `중복=` 표시가 정상 출력되는지 확인
   - `image_map.tsv`, `image_hashes.tsv`의 경로가 모두 `YYYY/MM/` 패턴인지 확인
4. **재실행 일관성**: 같은 명령을 두 번 실행하여 두 번째 실행 시 `기존=N`으로 표시되고, 파일 이동이 발생하지 않는지 확인
