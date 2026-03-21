# download_images.py 5가지 변경 계획

## Context
블로그 스크래퍼의 다운로드/수집 로직 개선:
1. zip 등 압축 파일 링크 다운로드 허용
2. 특정 포스트의 비이미지 링크 실패 기록 방지
3. Wayback Machine 조회 시 `?ref=` 파라미터 제거
4. `forms.gle` 등 다운로드 대상이 아닌 도메인 제외
5. Content-Disposition 헤더의 한글 파일명 깨짐 수정

## 대상 파일
- [download_images.py](download_images.py) (유일한 수정 파일)

---

## 변경 1: 압축 파일 다운로드 허용

### 1-1. 상수 추가 (line 48 이후)
```python
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}
DOWNLOADABLE_EXTS = IMG_EXTS | ARCHIVE_EXTS
```

### 1-2. 아카이브 Content-Type 헬퍼 추가 (`_is_image_ct` 이후, ~line 594)
```python
_ARCHIVE_CONTENT_TYPES = {
    "application/zip", "application/x-zip-compressed",
    "application/x-rar-compressed", "application/x-7z-compressed",
    "application/gzip", "application/x-tar",
    "application/octet-stream",
}

def _is_archive_ct(content_type: str) -> bool:
    return content_type.lower().split(";")[0].strip() in _ARCHIVE_CONTENT_TYPES
```

### 1-3. `_response_to_image()` 에 `allow_archive` 파라미터 추가 (line 597)
- `allow_archive=True`이면 아카이브 Content-Type 또는 아카이브 확장자도 허용

### 1-4. `_fetch_image()`, `_fetch_wayback_image()` 에 `allow_archive` 전파 (lines 621, 631)

### 1-5. 링크 수집 시 `IMG_EXTS` → `DOWNLOADABLE_EXTS` 변경
- line 1340: `linked_direct` 수집 조건
- line 1380, 1383: `_determine_filename()` 내 확장자 체크

### 1-6. 다운로드 디스패치에 `allow_archive=True` 추가
- lines 1708-1713: `linked_keyword` 분기
- lines 1715-1718: `linked_direct` 분기

### 1-7. 기존 파일 스캔 시 아카이브도 포함
- line 274: 해시 빌드 — `IMG_EXTS` → `DOWNLOADABLE_EXTS`
- line 466: image_map 보정 — `IMG_EXTS` → `DOWNLOADABLE_EXTS`

### 변경하지 않는 곳
- line 1325: `community-ko-cdn` `<img>` src 수집 — `<img>` 태그이므로 IMG_EXTS 유지

---

## 변경 2: --retry 시 비이미지 다운로드 링크(BGM 등) 가짜 실패 제거

### 2-1. 비이미지 컨텍스트 키워드 상수 추가 (~line 57)
```python
_NON_IMAGE_CONTEXT_KEYWORDS = {"bgm", "ost", "음악", "사운드트랙", "soundtrack"}
```

### 2-2. 비이미지 URL 감지 함수 추가 (`collect_image_urls` 인근)
```python
def _detect_non_image_urls(soup: BeautifulSoup, post_url: str) -> set[str]:
    """주변 컨텍스트에서 BGM 등 비이미지 키워드가 감지된 다운로드 URL을 반환한다."""
    skip_urls: set[str] = set()
    content_tag = (
        soup.select_one(".gh-content") or soup.select_one(".post-content")
        or soup.select_one("article") or soup.find("main") or soup
    )
    for anchor in content_tag.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:")):
            continue
        abs_href = urllib.parse.urljoin(post_url, href)
        parsed = urllib.parse.urlparse(abs_href)
        if parsed.hostname not in GDRIVE_HOSTS:
            continue
        # 앞쪽 소제목/강조 태그에서 비이미지 키워드 검색
        for prev in anchor.find_all_previous(["h1","h2","h3","h4","h5","h6","strong"]):
            text = prev.get_text(strip=True).lower()
            if any(kw in text for kw in _NON_IMAGE_CONTEXT_KEYWORDS):
                skip_urls.add(clean_url(abs_href))
                break
            break  # 가장 가까운 하나만 확인
    return skip_urls
```

### 2-3. `ImageFailedLog.remove()`에 `img_url` 파라미터 추가 (line 162)
현재 `reason`만으로 필터링하므로, 특정 img_url만 제거할 수 있도록 확장:
```python
def remove(self, post_url: str, reason: str | None = None, img_url: str | None = None) -> None:
```
`img_url`이 지정되면 해당 URL의 엔트리만 제거.
`remove_from_failed()` 래퍼도 `img_url` 파라미터 전달.

### 2-4. `process_post()`에서 retry 모드 시 적용 (line 1910 이전)
retry 모드 파라미터 추가 후, 이미지 다운로드 루프 전에:
```python
non_image_urls: set[str] = set()
if retry_mode:
    non_image_urls = _detect_non_image_urls(soup, post_url)
    for skip_url in non_image_urls:
        remove_from_failed(post_url, img_url=skip_url)
```

다운로드 루프에서 `clean_url(img_url) in non_image_urls`이면 건너뛰기.

### 2-5. `process_post()`에 `retry_mode` 파라미터 추가 + 기존 fallback 플래그 통합
기존 `multilang_fallback`, `kakao_pf_fallback`을 `retry_mode: bool = False` 하나로 통합:
- `process_post()` 시그니처에서 `multilang_fallback`, `kakao_pf_fallback` 제거, `retry_mode` 추가
- 내부에서 `retry_mode and multilang_date_index`로 multilang 활성화 판단
- 내부에서 `retry_mode and kakao_pf_index`로 kakao pf 활성화 판단
- 내부에서 `retry_mode`로 non-image cleanup 활성화 판단
- `run()` 내 `multilang_fallback = retry_mode`, `kakao_pf_fallback = retry_mode` 변수 제거
- `executor.submit()` 호출에서 `retry_mode=retry_mode` 전달
- `download_one_image()` 호출 시에도 동일하게 `multilang_fallback` → `retry_mode` 반영

---

## 변경 4: 다운로드 대상이 아닌 도메인 제외

`linked_keyword` 수집 시 `forms.gle`, `play.google.com` 등 다운로드 대상이 아닌 도메인은 건너뛰기.

### 4-1. 제외 도메인 상수 추가 (~line 57)
```python
_SKIP_LINK_HOSTS = {"forms.gle", "forms.google.com", "play.google.com", "apps.apple.com"}
```

### 4-2. `collect_image_urls()` 앵커 루프에서 제외 (line 1336 이후, gdrive 체크 다음)
```python
if parsed.hostname in _SKIP_LINK_HOSTS:
    continue
```
gdrive 체크(`if parsed.hostname in GDRIVE_HOSTS`) 바로 다음, `path_ext` 체크 전에 삽입.

---

## 변경 3: Wayback 조회 시 `?ref=` 제거

### 3-1. 헬퍼 함수 추가 (`_wayback_oldest` 직전, ~line 508)
```python
def _strip_ref_param(url: str) -> str:
    """URL에서 ?ref=... 파라미터를 제거한다."""
    return url.split("?ref=")[0] if "?ref=" in url else url
```

### 3-2. `_wayback_oldest()` 첫 줄에 적용 (line 509)
```python
url = _strip_ref_param(url)
```
캐시 키도 자동으로 정리된 URL을 사용하게 됨.

---

## 변경 5: 한글 파일명 깨짐 수정

### 원인
`requests` 라이브러리가 HTTP 헤더를 latin-1으로 디코딩 (HTTP 스펙). Google Drive 등에서 `Content-Disposition: filename="아발론특전대.png"`처럼 raw UTF-8을 보내면, `_filename_from_cd()`가 latin-1로 해석된 깨진 문자열을 반환.
예: `아발론특전대.png` → `ì\x95\x84ë°\x9cë¡\xa0í\x8a¹ì\xa0\x84ë\x8c\x80.png`

### 5-1. `_filename_from_cd()` 수정 (line 587-589)
`filename=` 매칭 후, latin-1 → UTF-8 재디코딩 시도:
```python
match = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.IGNORECASE)
if match:
    name = match.group(1).strip().strip('"')
    try:
        name = name.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return name
```

### 5-2. `_basename()` 수정 (line 338-340)
percent-encoding 디코딩 추가 (한글 등 URL 인코딩된 파일명 처리):
```python
def _basename(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = Path(path).name or ""
    return urllib.parse.unquote(name) if name else ""
```

### 5-3. 기존 깨진 파일명 일괄 수정 함수 추가
`_rename_fallback_images()` 패턴을 참고하여 `_fix_mojibake_filenames()` 함수 추가.
`_rename_fallback_images` 인근(~line 1453)에 배치.

```python
def _fix_mojibake_filenames(
    img_hashes: dict[str, str],
    image_map: dict[str, str],
) -> int:
    """latin-1로 깨진 한글 파일명을 UTF-8로 복원한다."""
    if not IMAGES_DIR.exists():
        return 0
    renamed = 0
    for file_path in IMAGES_DIR.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in DOWNLOADABLE_EXTS:
            continue
        old_name = file_path.name
        try:
            new_name = old_name.encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
        if new_name == old_name:
            continue
        new_path = file_path.parent / _safe_filename(new_name)
        if new_path.exists():
            continue
        file_path.rename(new_path)
        old_rel = file_path.relative_to(ROOT_DIR).as_posix()
        new_rel = new_path.relative_to(ROOT_DIR).as_posix()
        # image_map 갱신
        for key, val in list(image_map.items()):
            if val == old_rel:
                image_map[key] = new_rel
        # img_hashes 갱신
        for h, val in list(img_hashes.items()):
            if val == old_rel:
                img_hashes[h] = new_rel
        renamed += 1
    # TSV 파일 전체 재작성
    if renamed > 0:
        _rewrite_tsv(IMAGE_MAP_FILE, image_map)
        _rewrite_tsv_hashes(IMG_HASH_FILE, img_hashes)
        print(f"[FIX] mojibake filenames renamed={renamed}")
    return renamed
```

TSV 재작성은 기존 파일을 읽어서 old_rel → new_rel 치환하는 방식으로 구현.
(`image_map.tsv`는 `key\trel_path`, `image_hashes.tsv`는 `hash\trel_path\tflag` 형식)

### 5-4. 호출 시점
`run()` 함수 내에서 `_rename_fallback_images()` 호출 직후에 `_fix_mojibake_filenames()` 호출.

### 참고
- `----------20201016.jpg` 같은 대시 파일명은 Ghost CMS가 한글을 대시로 치환한 원본 URL 자체의 문제 — 스크래퍼 수정 불가

---

## 검증 방법
1. `python run_all.py --images --retry --sample 10` 실행하여 정상 동작 확인
2. `failed_images.txt`에서 `post202107221300` 관련 `linked_keyword` 실패가 없는지 확인
3. `url.kr` 같은 `?ref=` 포함 URL이 Wayback에서 정상 조회되는지 로그 확인
4. `forms.gle` 링크가 수집 대상에서 제외되는지 확인
5. Google Drive 등에서 한글 파일명이 정상 저장되는지 확인
