# Plan: 사이트맵 직접 fetch + retry-alt 옵션 + 폴백 이미지 접두사

## Context
1. EN/JA 사이트맵을 Wayback 경유 → XML 파싱 실패. 직접 fetch로 변경 필요
2. --retry 시 KakaoPF만 성공, multilang alt 누락 → failed_images에서 제거되어 보충 불가 → 전용 보충 옵션 필요
3. 폴백으로 확보한 이미지에 출처 접두사(`[Kakao]`, `[EN]`, `[JA]`) 부여

## 수정 대상
- [download_images.py](download_images.py) — 사이트맵 fetch, 접두사 적용, 보충 모드
- [run_all.py](run_all.py) — CLI 옵션 전달

---

## 수정 1: 사이트맵 직접 fetch

**위치**: [download_images.py:797-829](download_images.py#L797-L829) `_build_multilang_date_index()`

- `_wayback_oldest(sitemap_url)` 호출 제거 (line 805)
- Wayback 스냅샷 없음 분기 제거 (line 806-808)
- `sitemap_url`을 직접 `fetch_with_retry()`에 전달
- docstring 업데이트

---

## 수정 2: 폴백 이미지 파일명에 출처 접두사

### 2a. `_save_alternative_image()` 수정 ([line 1328](download_images.py#L1328))

`source_tag` 파라미터 추가. 파일명 앞에 접두사를 붙임:

```python
def _save_alternative_image(
    content: bytes,
    filename: str,
    folder: Path,
    source_tag: str = "",  # "[Kakao]", "[EN]", "[JA]"
) -> str | None:
    if source_tag:
        filename = f"{source_tag} {filename}"
    ...
```

### 2b. `download_one_image()` 내 alt 저장 시 접두사 결정 ([line 1493-1516](download_images.py#L1493-L1516))

- KakaoPF 소스 → `"[Kakao]"`
- multilang 소스 → URL에서 lang 판별 → `"[EN]"` 또는 `"[JA]"`

primary가 폴백인 경우에도 접두사 적용 필요. `_determine_filename()` 호출 후 `source_tag`를 prefix.

### 2c. primary 이미지가 폴백인 경우의 접두사 ([line 1442-1488](download_images.py#L1442-L1488))

payload가 폴백 결과에서 왔을 때(primary_source != None), 저장되는 파일명에도 접두사를 붙여야 함:

```python
if primary_source is not None:
    if primary_source.startswith("http://pf.kakao.com/"):
        source_tag = "[Kakao]"
    elif "blog-en" in primary_source:
        source_tag = "[EN]"
    elif "blog-ja" in primary_source:
        source_tag = "[JA]"
    else:
        source_tag = ""
    if source_tag:
        safe_name = f"{source_tag} {safe_name}"
```

---

## 수정 3: 기존 폴백 이미지 일괄 rename

기존 `kakao_pf_log.tsv`와 `multilang_fallback.tsv`에 기록된 이미지 파일들에 접두사가 없으므로, 일괄 rename 기능 추가.

### 전용 함수 `_rename_fallback_images()`

```python
def _rename_fallback_images():
    """기존 폴백 로그의 이미지에 출처 접두사가 없으면 rename한다."""
    updated_lines = []  # 로그 파일도 갱신

    for log_file, default_tag in [
        (KAKAO_PF_LOG_FILE, "[Kakao]"),
        (MULTILANG_LOG_FILE, None),  # source URL로 판별
    ]:
        if not log_file.exists():
            continue
        lines = log_file.read_text(encoding="utf-8").splitlines()
        new_lines = []
        for line in lines:
            parts = line.split("\t")
            rel_path, post_url, source = parts[0], parts[1], parts[2]

            # 이미 접두사 있으면 skip
            basename = Path(rel_path).name
            if basename.startswith("["):
                new_lines.append(line)
                continue

            # 접두사 결정
            if default_tag:
                tag = default_tag
            elif "blog-en" in source:
                tag = "[EN]"
            elif "blog-ja" in source:
                tag = "[JA]"
            else:
                new_lines.append(line)
                continue

            # rename
            old_path = ROOT_DIR / rel_path
            new_name = f"{tag} {basename}"
            new_path = old_path.parent / new_name
            if old_path.exists() and not new_path.exists():
                old_path.rename(new_path)
                new_rel = (new_path).relative_to(ROOT_DIR).as_posix()
                new_lines.append(f"{new_rel}\t{post_url}\t{source}")
            else:
                new_lines.append(line)

        log_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
```

**실행 시점**: `run_images()` 시작 시 또는 별도 CLI 옵션으로 호출. `--retry` 시작 시 자동 실행이 자연스러움.

---

## 수정 4: --retry-multilang / --retry-kakaopf 보충 옵션

### 동작 방식
- `--retry-multilang`: `kakao_pf_log.tsv`에 기록된 포스트를 대상으로, multilang에서 alt 이미지 보충
- `--retry-kakaopf`: `multilang_fallback.tsv`에 기록된 포스트를 대상으로, KakaoPF에서 alt 이미지 보충
- `--retry`와 독립적으로 실행 가능

### 구현: `_supplement_alt_images()` 함수

```python
def _supplement_alt_images(
    mode: str,  # "multilang" or "kakaopf"
    posts: list[tuple[str, str]],
    html_index: ...,
    max_workers: int,
):
```

1. 소스 로그 파일 읽기:
   - `mode == "multilang"` → `kakao_pf_log.tsv` (KakaoPF만 성공한 것)
   - `mode == "kakaopf"` → `multilang_fallback.tsv` (multilang만 성공한 것)

2. 로그에서 `post_url` 추출 → `set`으로 중복 제거

3. 보충 인덱스 구축:
   - `mode == "multilang"` → `_build_multilang_date_index()` 호출
   - `mode == "kakaopf"` → `_build_kakao_pf_index()` 호출

4. 대상 포스트별 처리 (ThreadPoolExecutor):
   - `fetch_post_html()` → `collect_image_urls()` → 이미지 목록 재구성
   - 로그에 기록된 이미지(rel_path 매칭)에 대해 반대쪽 폴백 시도
   - 성공 시 `_save_alternative_image(source_tag=...)` + 보충 로그에 기록

### CLI 옵션

**[download_images.py:1834-1847](download_images.py#L1834-L1847)**:
```python
parser.add_argument("--retry-multilang", action="store_true")
parser.add_argument("--retry-kakaopf", action="store_true")
```

**[run_all.py:222](run_all.py#L222)**:
```python
parser.add_argument("--retry-multilang", action="store_true",
                    help="KakaoPF 성공 이미지에 multilang alt 보충")
parser.add_argument("--retry-kakaopf", action="store_true",
                    help="multilang 성공 이미지에 KakaoPF alt 보충")
```

**[run_all.py:364](run_all.py#L364)** run_images 호출에 전달.

---

## Verification
1. `python run_all.py images --retry` → EN/JA 사이트맵 직접 fetch 성공, 파싱 오류 없음
2. 폴백 이미지에 `[Kakao]`, `[EN]`, `[JA]` 접두사가 파일명에 포함되는지 확인
3. `python run_all.py images --retry-multilang` → kakao_pf_log.tsv 기반 multilang alt 보충
4. `python run_all.py images --retry-kakaopf` → multilang_fallback.tsv 기반 KakaoPF alt 보충
5. 기존 폴백 이미지가 접두사 포함 이름으로 rename되었는지 확인
