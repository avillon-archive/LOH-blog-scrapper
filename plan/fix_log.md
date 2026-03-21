# retry 로그 안전성 강화 + --force 옵션

## Context
retry 시 failed 일괄삭제 버그로 실패 기록이 사라지는 문제 발생.
유저가 이미지 폴더를 삭제하고 새로 다운로드 예정. 마이그레이션 불필요.
향후 동일 버그 방지를 위한 로그 시스템 보강과 로직 오류 수정.

## 대상 파일
- [download_images.py](download_images.py) — retry failed 제거 로직 수정
- [run_all.py](run_all.py) — `--force` CLI 옵션 추가

---

## 수정 1: retry 시 failed 개별 URL 기반 제거

### PostProcessResult에 succeeded_urls 추가
```python
@dataclass
class PostProcessResult:
    ok: int
    fail: int
    post_fetch_ok: bool
    ok_original: int = 0
    ok_multilang: int = 0
    ok_kakao: int = 0
    succeeded_urls: list = field(default_factory=list)
```

### process_post에서 성공 URL 기록
다운로드 루프에서 `how`가 truthy이면 `succeeded_urls.append(img_url)` (already 포함).

### run()에서 일괄 삭제 → 개별 삭제로 변경
```python
# Before:
if result.fail == 0 and result.ok > 0:
    remove_from_failed(post_url, reason="download_failed")

# After:
for url in result.succeeded_urls:
    remove_from_failed(post_url, img_url=url)
```

`fetch_post_failed` 제거는 기존대로 유지 (포스트 fetch 성공 시 제거).

---

## 수정 2: `--force` CLI 옵션 추가

### run_all.py에 옵션 추가
```python
parser.add_argument("--force", action="store_true",
                    help="기존 기록 무시하고 전체 재다운로드 (done 기록 무시)")
```

### force_download 연동
```python
force_download = args.custom or args.force
```

`--force`는 `--custom`과 달리 포스트 소스를 바꾸지 않음 — 기존 all_links.txt 사용하되 done 기록만 무시.

---

---

## 수정 3: retry 모드에서 done_post_urls 무시

현재 retry 대상 포스트가 `done_post_urls`에 있으면 건너뜀 (line 2060).
하지만 retry 대상 = failed 엔트리가 있는 포스트이므로, done 상태와 모순.

**수정**: retry 모드에서 `done_post_urls`를 비움:
```python
done_post_urls: set[str] = set() if (force_download or retry_mode) else _load_done_post_urls(DONE_POSTS_FILE)
```

---

## 수정 4: `done_posts_images.txt`에 이미지 수 기록

현재 포스트 URL만 저장 → 수집 로직 변경 시 미완료인데도 done 처리 가능.

### 형식 변경
```
post_url\timage_count
```

### `_done_posts_buf.add()` 호출 시 수집된 이미지 수 포함
```python
if fail == 0:
    done_post_urls.add(post_url)
    _done_posts_buf.add(f"{post_url}\t{len(images)}")
```

### `_load_done_post_urls()` 수정
기존 형식(URL만)과 새 형식(URL\tcount) 모두 호환:
```python
def _load_done_post_urls(filepath: Path) -> dict[str, int]:
    """포스트 URL → 이미지 수 딕셔너리를 반환한다."""
    result = {}
    ...
    for line in ...:
        parts = line.split("\t")
        url = parts[0].strip()
        count = int(parts[1]) if len(parts) >= 2 else -1  # 레거시는 -1
        result[url] = count
    return result
```

### `process_post()` 진입부에서 이미지 수 비교
로컬 HTML 사용 시 네트워크 비용 없음 (디스크 I/O + 파싱만). 이미지 수가 달라졌으면 재처리:
```python
if post_url in done_post_urls:
    stored_count = done_post_urls[post_url]
    if stored_count >= 0:
        # 이미지 수 비교: HTML에서 현재 수집 가능한 이미지 수와 비교
        html_text = fetch_post_html(post_url, html_index)
        if html_text is not None:
            soup = BeautifulSoup(html_text, "lxml")
            current_count = len(collect_image_urls(soup, post_url))
            if current_count != stored_count:
                pass  # 개수 불일치 → 아래로 계속 진행 (재처리)
            else:
                return PostProcessResult(ok=0, fail=0, post_fetch_ok=True)
        else:
            return PostProcessResult(ok=0, fail=0, post_fetch_ok=True)
    else:
        # 레거시 엔트리 (count 없음) → 기존 동작 유지 (스킵)
        return PostProcessResult(ok=0, fail=0, post_fetch_ok=True)
```

이렇게 하면 수집 로직이 변경되어 이미지 수가 달라진 포스트는 자동 재처리됨.

---

## 수정 5: `clean_url`에 `_strip_ref_param` 통합

URL 정규화가 `clean_url`과 `_strip_ref_param` 두 곳에 분산.
`clean_url`이 `_strip_ref_param`도 포함하면, `_seen_key`, `image_map`, `failed_images` 모두 일관된 키를 사용.

### utils.py의 `clean_url` 수정 불가 (다른 모듈도 사용)
→ `download_images.py`에서 `_clean_img_url()` 래퍼를 정의:
```python
def _clean_img_url(url: str) -> str:
    return clean_url(_strip_ref_param(url))
```

### 적용 위치
- `_seen_key()`: `clean_url(url)` → `_clean_img_url(url)`
- `collect_image_urls._add()`: `clean_url(url)` → `_clean_img_url(url)`
- `download_one_image`의 `img_key = clean_url(img_url)` → `_clean_img_url(img_url)`

→ 이미 `_add()`에서 `_strip_ref_param`을 먼저 적용하고 있으므로, `_seen_key`와 `img_key`에서도 동일하게 적용하면 완전한 일관성 확보.

---

## 검증
1. `python run_all.py --images --force` 로 전체 재다운로드 가능 확인
2. retry 시 실제 성공한 URL만 failed에서 제거되는지 확인
3. retry 대상 포스트가 done_post_urls에 있어도 건너뛰지 않는지 확인
4. `done_posts_images.txt`에 이미지 수가 기록되는지 확인
