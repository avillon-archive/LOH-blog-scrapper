# Kakao PF Fallback 구현 계획

## Context

`--images --retry` 시 multilang wayback fallback까지 실패한 이미지에 대해,
Kakao PF API(`pf.kakao.com/rocket-web/web/profiles/_YXZqxb/posts`)를 최종 fallback으로 추가.

- Kakao PF API는 인증 없이 호출 가능 (200 OK 확인 완료)
- 이미지는 블로그와 동일/유사한 이미지가 Kakao CDN에 별도 업로드됨
- media 객체의 `url` 필드가 원본 (xlarge 등은 리사이즈된 JPG)

## API 구조

```
목록: GET /rocket-web/web/profiles/_YXZqxb/posts?includePinnedPost=true[&since={sort}]
개별: GET /rocket-web/web/profiles/_YXZqxb/posts/{postId}
```

- 페이징: `has_next` + `since` (마지막 item의 `sort` 값), 페이지당 20개
- 포스트 필드: `id`, `title`, `published_at` (Unix ms), `media[]`, `contents[]`, `sort`
- media: `type`, `url` (원본), `width`, `height`, `mimetype`
- link 타입 media: `images[].url` 필드에 이미지 포함

## 수정 대상

**`download_images.py`** — 유일한 수정 파일

## 구현 상세

### 1. 상수 추가 (상단)

```python
KAKAO_PF_PROFILE = "_YXZqxb"
KAKAO_PF_API = f"https://pf.kakao.com/rocket-web/web/profiles/{KAKAO_PF_PROFILE}/posts"
```

### 2. `_build_kakao_pf_index()` 함수

- retry 모드 시작 시 1회 호출 (multilang index 구축과 유사 패턴)
- **JSON 캐시**: `kakao_pf_index.json`에 저장
  - 파일 존재 시 로드 후, 마지막 `sort` 값 이후의 새 포스트만 추가 fetch
  - 파일 미존재 시 전체 페이지네이션 (~1300개, ~65 API 호출)
  - 인덱스 갱신 후 JSON 파일 재작성
- 반환: `dict[str, list[KakaoPFPost]]` — key는 날짜 문자열 (YYYY-MM-DD)
- `KakaoPFPost`: `NamedTuple(id, title, published_at, media_urls: list[str])`
  - `media_urls`는 `url` 필드만 수집 (원본)
  - link 타입 media의 경우 `images[0].url`도 수집
- rate limiting: 기존 `utils.py`의 `_rate_limit()` 또는 단순 sleep(0.2) 사용
- 실패 시 빈 dict 반환 (Kakao API 접속 불가 시 graceful 처리)

**JSON 캐시 구조:**
```json
{
  "last_sort": "115315075253046360",
  "posts": [
    {"id": 111533189, "title": "...", "published_at": 1766566810000, "media_urls": ["http://..."]}
  ]
}
```

### 3. `_fetch_kakao_pf_image()` 함수

시그니처:
```python
def _fetch_kakao_pf_image(
    post_url: str,
    img_url: str,
    post_date: str,
    utype: str,
    idx: int,
    kakao_pf_index: dict[str, list],
) -> tuple[bytes, str, str, str, str] | None:
```

로직:
1. `post_date`로 `kakao_pf_index[post_date]` 조회 → 후보 포스트 목록
2. 같은 날짜에 여러 포스트 → 블로그 포스트 제목과 Kakao 제목 유사도로 선택
   - 제목 유사도: 간단한 문자열 포함 검사 또는 공통 키워드 비교
   - 후보가 1개면 바로 사용
3. 선택된 포스트의 `media_urls`에서 이미지 매칭:
   - `og_image` (utype) → 첫 번째 이미지
   - `img` → position 기반 (idx 매칭, 기존 multilang Phase B와 동일 전략)
4. 매칭된 URL로 `_fetch_image()` 호출
5. 성공 시 `(content, final_url, content_type, disposition, "kakao_pf")` 반환

### 4. `download_one_image()` 통합 (L1147~1160 부근)

Kakao PF와 multilang을 **둘 다 독립적으로** 시도하고, 둘 다 성공 시 모두 저장:

```python
# ── Kakao PF 폴백 (retry 모드 전용) ────────────────────────────
kakao_pf_payload = None
if payload is None and kakao_pf_fallback and utype != "linked_direct":
    kp_result = _fetch_kakao_pf_image(...)
    if kp_result is not None:
        kakao_pf_payload = kp_result

# ── 다국어 Wayback 폴백 (retry 모드 전용) ──────────────────────
multilang_payload = None
if payload is None and multilang_fallback and utype != "linked_direct":
    ml_result = _fetch_multilang_wayback_image(...)
    if ml_result is not None:
        multilang_payload = ml_result

# ── 둘 다 성공 시 모두 저장 ────────────────────────────────────
if payload is None and kakao_pf_payload and multilang_payload:
    # 파일 크기 큰 쪽을 primary로, 작은 쪽을 alternative로
    kp_size = len(kakao_pf_payload[0])
    ml_size = len(multilang_payload[0])
    if kp_size >= ml_size:
        payload = kakao_pf_payload[:4]
        alt_payload = multilang_payload
        primary_source, alt_source = "kakao_pf", multilang_payload[4]
    else:
        payload = multilang_payload[:4]
        alt_payload = kakao_pf_payload
        primary_source, alt_source = multilang_payload[4], "kakao_pf"
    # alt_payload → _save_alternative_image() 로 별도 저장
elif payload is None and kakao_pf_payload:
    payload = kakao_pf_payload[:4]
    primary_source = "kakao_pf"
elif payload is None and multilang_payload:
    payload = multilang_payload[:4]
    primary_source = multilang_payload[4]
```

### 4-1. `_save_alternative_image()` 함수

- alternative 이미지를 **primary와 같은 폴더**에 저장 (파일명 충돌은 `save_image()`의 `_2` 접미사로 자동 해소)
- `image_map`에는 추가하지 않음 (마크다운은 primary만 참조)
- `kakao_pf_log.tsv` 또는 `multilang_fallback_log.tsv`에 기록하여 어떤 이미지가 alternative인지 추적
- 해시 중복 체크는 primary와 동일하게 수행 (완전 동일 이미지면 skip)

### 5. `process_post()` 파라미터 추가

- `kakao_pf_fallback: bool = False`
- `kakao_pf_index: dict | None = None`
- `download_one_image()` 호출 시 전달

### 6. `run_images()` 통합

multilang index 구축 직후:
```python
kakao_pf_fallback = retry_mode
kakao_pf_index: dict[str, list] = {}
if kakao_pf_fallback:
    print("[이미지] Kakao PF 폴백 활성화: 게시글 인덱스 구축 중...")
    kakao_pf_index = _build_kakao_pf_index()
    if kakao_pf_index:
        total_posts = sum(len(v) for v in kakao_pf_index.values())
        print(f"  Kakao PF 인덱스: {len(kakao_pf_index)}일, 총 {total_posts}개 포스트")
```

### 7. 로그 기록 — `kakao_pf_log.tsv`

Kakao PF fallback으로 이미지 다운로드 성공 시 전용 로그 파일 기록:

```
이미지경로\t블로그포스트URL\t카카오포스트URL
images/category/2025/12/img.png\thttps://blog-ko.lordofheroes.com/post202512241800/\thttp://pf.kakao.com/_YXZqxb/111533189
```

- `LineBuffer` 패턴 사용 (`_kakao_pf_log_buf`)
- 카카오 포스트 URL은 `permalink` 필드 또는 `f"http://pf.kakao.com/{KAKAO_PF_PROFILE}/{post_id}"` 로 생성
- `_multilang_log_buf`와 별도 파일로 분리 (source 구분 명확화)

## Fallback 체인 (최종)

```
1. Direct fetch (원본 URL)
2. Wayback CDX (oldest snapshot)
3. Wayback post HTML + URL matching
4. [RETRY] Kakao PF fallback  ← NEW (KO 이미지, 텍스트 포함 이미지에 유리)
5. [RETRY] Multilang Wayback (EN/JA, 이미지 내 텍스트가 다른 언어일 수 있음)
6. failed_images.txt 기록
```

**둘 다 시도:** Kakao PF(KO, 800x600 가능성)와 multilang(EN/JA, 고해상도 가능성)을
독립적으로 시도. 둘 다 성공 시 파일 크기가 큰 쪽이 primary, 작은 쪽은 `_alternatives/`에 저장.

## 제목 매칭 전략 (같은 날짜 다중 포스트 처리)

간단한 접근:
1. 블로그 포스트의 `<title>` 또는 `<h1>`에서 제목 추출 (이미 soup 사용 중)
2. Kakao PF 후보들의 `title`과 비교
3. 공통 단어 수 또는 `difflib.SequenceMatcher.ratio()` 사용
4. 가장 높은 유사도의 포스트 선택, 유사도가 모두 낮으면 skip

## 검증 방법

```bash
# 1. Kakao PF 인덱스 구축만 테스트 (빠른 확인)
python -c "from download_images import _build_kakao_pf_index; idx = _build_kakao_pf_index(); print(len(idx))"

# 2. failed_images.txt에 항목이 있는 상태에서 retry 실행
python run_all.py --images --retry --sample 5

# 3. 결과 확인: failed_images.txt 줄 수 감소 여부
```
