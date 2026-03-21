# --retry 0건 다운로드 버그 수정

## Context
이전 변경 후 `--images --retry` 실행 시 `성공=41 (원본=0 multilang=0 kakao=0) 실패=13`.
41건은 모두 "already" (이전 다운로드 캐시 히트), 새 다운로드 0건.
failed_images.txt 분석 결과 여러 원인 발견.

## 대상 파일
- [download_images.py](download_images.py)

---

## 버그 1: `ref` 파라미터 제거가 불완전

### 원인
Ghost CMS가 모든 외부 링크에 `?ref=blog-ko.lordofheroes.com` 또는 `&ref=blog-ko.lordofheroes.com`을 추가.
- `_strip_ref_param`이 `?ref=`만 처리 → `&ref=`는 제거 안 됨
- `_wayback_oldest()`에서만 적용 → 직접 fetch에는 미적용

### 수정
**URL 수집 시점에서 `ref` 파라미터를 제거**하는 것이 근본 해결:

1. `_strip_ref_param()` 을 `urllib.parse`로 개선:
```python
def _strip_ref_param(url: str) -> str:
    """URL에서 ref 쿼리 파라미터를 제거한다 (Ghost CMS 참조 추적용)."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.query:
        return url
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if "ref" not in params:
        return url
    params.pop("ref")
    new_query = urllib.parse.urlencode(params, doseq=True)
    return urllib.parse.urlunparse(parsed._replace(query=new_query))
```

2. `collect_image_urls()`의 `_add()` 내에서 URL 추가 전에 `_strip_ref_param` 적용:
```python
def _add(url: str, utype: str):
    url = _strip_ref_param(url)
    ...
```

3. `_wayback_oldest()` 내의 `url = _strip_ref_param(url)` 호출은 유지 (안전장치)

### 효과
- 모든 수집 URL에서 `ref` 제거 → 직접 fetch, Wayback, 비교 모두 깨끗한 URL 사용
- url.kr 직접 fetch가 `?ref=` 없이 시도됨 → 성공 가능성 증가
- Google Drive `&ref=` 포함 URL도 정상 동작

---

## 버그 2: Google Sheets/Forms가 gdrive로 수집됨

### 원인
`docs.google.com` 가 GDRIVE_HOSTS에 포함 → `docs.google.com/spreadsheets/...` 와 `docs.google.com/forms/...` 가 gdrive 타입으로 수집됨. 이들은 HTML 페이지(스프레드시트 뷰어, 폼)이므로 이미지/아카이브가 아님.

### 수정
`collect_image_urls()` 앵커 루프에서 gdrive 체크 시 경로 필터 추가:
```python
if parsed.hostname in GDRIVE_HOSTS:
    # Google Sheets 뷰어, Forms 등 비다운로드 URL은 제외
    path_lower = parsed.path.lower()
    if "/spreadsheets/" in path_lower or "/forms/" in path_lower:
        continue
    _add(abs_href, "gdrive")
    continue
```

`<img>` 태그 내 gdrive 체크에도 동일 적용 (단, img src에 spreadsheets는 거의 없으므로 선택적).

---

## 버그 3: `go.onelink.me` 미제외

### 수정
`_SKIP_LINK_HOSTS`에 추가:
```python
_SKIP_LINK_HOSTS = {"forms.gle", "forms.google.com", "play.google.com", "apps.apple.com",
                    "go.onelink.me"}
```

---

## 버그 4: `_fetch_wayback_linked_from_post`에 `allow_archive` 미전달

### 수정
시그니처에 `allow_archive` 추가, 내부 `_fetch_image` 호출에 전파.
`download_one_image`의 `linked_keyword` 분기에서 `allow_archive=True` 전달.

---

## 버그 5: Wayback redirect 원본 직접 시도 미지원

### 배경
url.kr → Google Drive 같은 URL 단축기 redirect의 경우,
Wayback 스냅샷 방문 시 redirect target(구글 링크)이 드러남.
이 원본 링크를 직접 fetch하면 Wayback im_ 경유보다 빠름.

### 수정
`_fetch_wayback_image()` 개선:
```python
def _fetch_wayback_image(url, ...):
    wayback_url = _wayback_oldest(url)
    if not wayback_url:
        return None
    # 1. Wayback URL을 follow redirect → 최종 URL에서 원본 대상 추출
    resp = fetch_with_retry(wayback_url, allow_redirects=True)
    if resp is not None:
        original_target = _original_url_from_wayback(resp.url)
        if original_target and _normalized_link_key(original_target) != _normalized_link_key(url):
            # 2. redirect 대상 원본을 직접 fetch (빠름)
            direct = _response_to_image(resp, allow_archive=..., ...)
            if direct is not None:
                return direct
            direct = _fetch_image(original_target, allow_archive=..., ...)
            if direct is not None:
                return direct
    # 3. 직접 실패 시 Wayback im_ 경유 폴백
    return _fetch_image(_add_im(wayback_url), ...)
```

핵심: Wayback에서 redirect를 따라가서 원본 URL을 발견하면, 먼저 직접 시도. 실패 시 Wayback im_ 경유.
Wayback 응답 자체가 이미지/아카이브인 경우 그대로 사용 (`_response_to_image`로 체크).

---

## 검증
1. `python run_all.py --images --retry` 실행
2. url.kr 링크가 직접 또는 Wayback으로 다운로드 성공하는지 확인 (원본 > 0)
3. Google Sheets/Forms/onelink URL이 수집에서 제외되는지 확인
4. 나머지 실패 건수 감소 확인
