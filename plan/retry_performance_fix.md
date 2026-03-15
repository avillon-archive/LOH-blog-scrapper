# Fix: --retry 시 workers/rate_limit이 실제 재처리 건수를 반영하지 않는 버그

## Context
`--retry` 모드에서 `[설정] workers=8, rate_limit=10 req/s` 로그가 소스 파일의 **전체 포스트 수** 기준으로 출력됨.
실제 재처리 대상 필터링은 각 pipeline step 내부(`run_pipeline`, `download_images`)에서 나중에 수행되므로, 설정값과 실제 동작이 불일치.

예: 소스 500건, 실패 30건 → 로그 `workers=8, rate_limit=10` 출력, 실제는 30건만 처리 → `workers=30, rate_limit=20`이 적절.

## 수정 대상
- [run_all.py](run_all.py) lines 347-351

## 수정 내용

retry 모드일 때, 3개 실패 파일(`FAILED_HTML_FILE`, `FAILED_IMAGES_FILE`, `FAILED_MD_FILE`)에서 URL을 로드하여 `posts`와의 교집합으로 실제 재처리 건수를 산출 → 이 건수 기준으로 `max_workers`/`rate_limit` 결정.

### 재사용할 기존 코드
- `load_failed_post_urls()` — 이미 [run_all.py:34](run_all.py#L34)에서 import됨
- `FAILED_HTML_FILE`, `FAILED_IMAGES_FILE`, `FAILED_MD_FILE` — [run_all.py:56-58](run_all.py#L56-L58)에 정의됨

### 변경 코드 (lines 347-351 교체)

```python
# ── 동적 워커 수·rate limit 설정 ──────────────────────────────────
if args.retry:
    retry_urls: set[str] = set()
    for fpath in (FAILED_HTML_FILE, FAILED_IMAGES_FILE, FAILED_MD_FILE):
        retry_urls |= load_failed_post_urls(fpath)
    post_urls = {url for url, _ in posts}
    effective_count = len(retry_urls & post_urls) or len(posts)
else:
    effective_count = len(posts)

max_workers = min(effective_count, 32) if effective_count <= 100 else DEFAULT_MAX_WORKERS
if effective_count <= 100:
    set_blog_rate_limit(BLOG_RATE_LIMIT_SMALL)  # 20 req/s
print(f"[설정] workers={max_workers}, rate_limit={'20' if effective_count <= 100 else '10'} req/s")
```

## 검증
1. `--retry` + 실패 건수 < 100 → `[설정]` 로그에 `workers=<실패건수>, rate_limit=20` 출력 확인
2. `--retry` + 실패 건수 > 100 → `workers=8, rate_limit=10` 출력 확인
3. 일반 모드 → 기존과 동일하게 동작 확인
4. 실패 파일 없을 때 → fallback으로 전체 posts 수 사용 확인
