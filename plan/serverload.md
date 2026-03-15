# 서버 부하 개선 계획

## Context

블로그 스크래퍼가 `blog-ko.lordofheroes.com`에 ~160 req/s 부하를 주고 있다. 32개 스레드가 글로벌 조율 없이 독립적으로 요청하고, 동일 포스트 HTML을 3개 파이프라인에서 중복 fetch하며, HTTP 429 처리가 없다.

**이미지의 93%가 블로그 도메인**에서 호스팅되므로(image_map.tsv 기준 6,764/7,274), 블로그 도메인 rate limiting이 이미지 다운로드에도 큰 영향을 준다. 이를 고려하여 rate limit을 10 req/s로 설정한다.

---

## Step 1: DEFAULT_MAX_WORKERS 동적 조절

**파일:** `utils.py:26`, `run_all.py`

- 기존: `DEFAULT_MAX_WORKERS = 32` (고정)
- 변경: 기본값 `8`, 포스트 100건 이하 시 `min(len(posts), 32)`
- `run_all.py`에서 포스트 수에 따라 `max_workers` 결정 후 각 stage에 전달

```python
# utils.py
DEFAULT_MAX_WORKERS: int = 8

# run_all.py — 포스트 수 기반 동적 조절
max_workers = min(len(posts), 32) if len(posts) <= 100 else DEFAULT_MAX_WORKERS
```

각 `run_*` 함수와 `run_pipeline`은 이미 `max_workers` 파라미터를 받으므로, 전달만 하면 됨.
`download_images.py`의 `run_images`도 `max_workers` 파라미터 확인 필요.

---

## Step 2: 블로그 도메인 토큰 버킷 Rate Limiter + REQUEST_DELAY 제거

**파일:** `utils.py`

### 2-1. `_TokenBucket` 클래스 추가

```python
BLOG_RATE_LIMIT: float = 10.0       # 대규모 배치 (>100건) 블로그 도메인 초당 최대 요청 수
BLOG_RATE_LIMIT_SMALL: float = 20.0  # 소규모 배치 (≤100건) 블로그 도메인 초당 최대 요청 수
BLOG_HOST: str = "blog-ko.lordofheroes.com"

class _TokenBucket:
    """Thread-safe token bucket rate limiter."""
    def __init__(self, rate: float, burst: int = 2):
        self._rate = rate
        self._burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(wait)

_blog_rate_limiter = _TokenBucket(BLOG_RATE_LIMIT)

def set_blog_rate_limit(rate: float) -> None:
    """소규모 배치용 rate limit 동적 변경."""
    global _blog_rate_limiter
    _blog_rate_limiter = _TokenBucket(rate)
```

`run_all.py`에서 포스트 수에 따라 호출:
```python
if len(posts) <= 100:
    set_blog_rate_limit(BLOG_RATE_LIMIT_SMALL)  # 20 req/s
```

### 2-2. `fetch_with_retry()` 수정 (lines 153-184)

- retry 루프 상단에서 URL 호스트 확인 → 블로그 도메인이면 `_blog_rate_limiter.acquire()`
- `time.sleep(REQUEST_DELAY)` 제거 (rate limiter가 대체)
- `REQUEST_DELAY` 상수 제거

```python
def fetch_with_retry(url, ...):
    parsed = urllib.parse.urlparse(url)
    is_blog = parsed.hostname == BLOG_HOST
    ...
    for attempt in range(3):  # → Step 3에서 while로 변경
        if is_blog:
            _blog_rate_limiter.acquire()
        try:
            resp = get_session().request(...)
            resp.raise_for_status()
            return resp  # REQUEST_DELAY 제거
        ...
```

**참고:** `BLOG_HOST` 상수가 `download_images.py`에 이미 존재함. `utils.py`로 이동하거나 import해서 공유.

---

## Step 3: HTTP 429 Adaptive Backoff

**파일:** `utils.py` — `fetch_with_retry()` 내부

- `for attempt in range(3)` → `while attempt < 3` 패턴으로 변경
- HTTPError 처리에서 429 감지:
  - `Retry-After` 헤더 값 존중 (최대 120초 cap)
  - 헤더 없으면 `5 * (attempt + 1)` 초 대기 (최대 60초)
  - **429는 retry 횟수를 소모하지 않음** (`continue`, attempt 미증가)
- 404/410은 기존대로 즉시 포기

---

## Step 4: 파이프라인 순서 변경 + HTML 파일 재활용

현재 images/md/html 3개 파이프라인이 동일 포스트 HTML을 각각 fetch → 총 6,600회.
html 단계를 항상 먼저 실행하고, 저장된 html 파일을 md/images에서 읽어 2,200회로 감소.

### 4-1. 파이프라인 순서 변경

**파일:** `run_all.py`

- `PIPELINE_ORDER = ("images", "md", "html")` → `("html", "images", "md")`
- html 단계는 **항상 실행**:
  ```python
  if not selected:
      selected = set(PIPELINE_ORDER)
  selected.add("html")  # html은 항상 실행
  ```

### 4-2. HTML 인덱스 빌더 함수

**파일:** `utils.py`

html 단계 완료 후 `done_html.txt` + 파일시스템 스캔으로 `{post_url → Path}` 매핑:

```python
def build_html_index(html_dir: Path, done_file: Path) -> dict[str, Path]:
    """done_html.txt와 html 디렉토리를 스캔하여 {post_url: html_path} 매핑을 반환."""
    done_map = load_done_file(done_file)  # {slug: url}
    slug_to_path: dict[str, Path] = {}
    for html_file in html_dir.rglob("*.html"):
        slug_to_path[html_file.stem] = html_file
    index: dict[str, Path] = {}
    for slug, url in done_map.items():
        if slug in slug_to_path:
            index[url] = slug_to_path[slug]
    return index
```

### 4-3. `fetch_post_html()` 헬퍼 함수

**파일:** `utils.py`

```python
def fetch_post_html(url: str, html_index: dict[str, Path] | None = None) -> str | None:
    """html_index에서 로컬 파일 먼저 확인, 없으면 서버 fetch."""
    if html_index is not None and url in html_index:
        path = html_index[url]
        if path.exists():
            return path.read_text(encoding="utf-8")
    resp = fetch_with_retry(url)
    return resp.text if resp is not None else None
```

### 4-4. `run_all.py` — html 실행 후 인덱스 전달

```python
html_index = None
max_workers = min(len(posts), 32) if len(posts) <= 100 else DEFAULT_MAX_WORKERS

for step in selected_order:
    if step == "html":
        run_html(posts, ..., max_workers=max_workers)
        html_index = build_html_index(HTML_DIR, DONE_HTML_FILE)
    elif step == "images":
        run_images(posts, ..., html_index=html_index, max_workers=max_workers)
    elif step == "md":
        run_md(posts, ..., html_index=html_index, max_workers=max_workers)
```

### 4-5. 각 파이프라인 수정

| 파일 | 변경 |
|------|------|
| `download_images.py` | `run_images`에 `html_index` 파라미터 추가, `process_post`에서 `fetch_post_html(url, html_index)` 사용 |
| `download_md.py` | 동일 패턴 |
| `download_html.py` | 변경 없음 (항상 먼저 실행) |

---

## 예상 효과

| 지표 | 현재 | 개선 후 |
|------|------|--------|
| 워커 수 | 32 | 8 (대규모) / 최대 32 (소규모) |
| 블로그 도메인 req/s | ~160 | 10 (대규모) / 20 (소규모) |
| 포스트 HTML fetch | 6,600 | 2,200 (html 재활용) |
| 블로그 도메인 총 요청 | ~9,000+ | ~6,800 (HTML 캐싱 효과) |
| 429 대응 | 없음 | Retry-After 존중 |
| **예상 소요 시간 (~2,200 포스트)** | **~5분** | **~12분** |

---

## 수정 대상 파일

- **`utils.py`** — Step 1~4 전체 (핵심: `DEFAULT_MAX_WORKERS`, `_TokenBucket`, `fetch_with_retry`, `build_html_index`, `fetch_post_html`, `REQUEST_DELAY` 제거)
- **`download_images.py`** — Step 4 (`process_post`에서 `fetch_post_html` 사용, `run_images`에 `html_index`/`max_workers` 파라미터)
- **`download_md.py`** — Step 4 (동일 패턴)
- **`download_html.py`** — 변경 없음
- **`run_all.py`** — Step 1, 4 (`PIPELINE_ORDER` 변경, html 항상 실행, 동적 워커, 인덱스 구축/전달)

## 검증

1. `python run_all.py --help`로 기존 CLI 옵션 정상 동작 확인
2. `python run_all.py --images`만 실행해도 html이 먼저 실행되는지 확인
3. images/md 단계에서 HTML fetch 없이 로컬 파일 읽기 확인 (로그 관찰)
4. `python run_all.py --sample 5`로 rate limiter 동작 확인 (요청 간격 관찰)
5. 429 응답 시뮬레이션 또는 실제 테스트로 backoff 동작 확인
