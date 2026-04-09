# log_io.py — CSV 로그 파일 I/O

모든 구분자 기반 로그 파일의 읽기/쓰기를 담당하는 전용 모듈. `utils.py`에서 분리.

## CSV 규약

- **인코딩**: utf-8-sig (BOM 포함) — Excel 호환.
- **헤더**: 모든 CSV 파일의 첫 행에 컬럼명 포함.
- **구분자**: 콤마. `csv` 모듈로 읽기/쓰기.

---

## 파일 인벤토리

### 포스트 목록

| 파일 | 헤더 | 생성자 |
|------|------|--------|
| `all_posts.csv` (+lang) | `url,lastmod,published_time` | `build_posts_list.py` |
| `all_pages.csv` (+lang) | `url,lastmod,published_time` | `build_posts_list.py` |
| `all_links.csv` (+lang) | `url,lastmod,published_time` | `build_posts_list.py` |

### 완료/실패 트래킹

| 파일 | 헤더 | Writer | Reader |
|------|------|--------|--------|
| `done_html.csv` (+lang) | `slug,post_url` | `write_text_unique` | `load_done_file` |
| `done_md.csv` | `slug,post_url` | `write_text_unique` | `load_done_file` |
| `done_html_local.csv` | `slug,post_url` | `append_line` | `load_done_file` |
| `failed_html.csv` (+lang) | `post_url,reason` | `FailedLog.record` | `load_failed_post_urls` |
| `failed_md.csv` | `post_url,reason` | `FailedLog.record` | `load_failed_post_urls` |
| `failed_html_local.csv` | `post_url,reason` | `FailedLog.record` | `load_failed_post_urls` |
| `failed_images.csv` | `post_url,img_url,reason` | `ImageFailedLog.record` | `load_failed_image_entries` |

### 이미지 트래킹

| 파일 | 헤더 | 비고 |
|------|------|------|
| `image_map.csv` | `clean_url,relative_path` | 정규화 URL → 저장 경로 |
| `image_hashes.csv` | `sha256_hash,relative_path,is_thumbnail` | SHA-256 중복 제거 |
| `done_posts_images.csv` | `post_url,image_count` | 포스트별 이미지 처리 완료 |
| `downloaded_urls.txt` | (헤더 없음, prefix 형식) | **CSV 대상 아님** |

### stale (재생성 대상)

| 파일 | 헤더 | 비고 |
|------|------|------|
| `stale_md.csv` | `post_url,unmapped_urls` | unmapped_urls는 `\|`로 구분 |
| `stale_html_local.csv` | `post_url,unmapped_urls` | 동일 |

### 폴백 (`--retry-fallback` 전용)

| 파일 | 헤더 |
|------|------|
| `fallback_image_map.csv` | `clean_url,relative_path` |
| `fallback_image_hashes.csv` | `sha256_hash,relative_path,is_thumbnail` |
| `fallback_multilang.csv` | `saved_path,post_url,source_url,original_img_url,phase` |
| `fallback_kakao_pf.csv` | `saved_path,post_url,source_url,original_img_url,phase` |
| `fallback_still_failed.csv` | `post_url,img_url,reason` |
| `fallback_report.csv` | `post_url,original_img_url,fallback_type,saved_path,source_url` |
| `fallback_downloaded_urls.txt` | (헤더 없음, prefix 형식) | **CSV 대상 아님** |

---

## API

### Writer

- **`csv_line(*fields) -> str`**: 단일 CSV 행 포맷. `csv.writer`로 quoting 처리.
- **`append_line(filepath, line, *, header=None)`**: 스레드 안전 1행 추가. `header` 지정 시 파일 미존재면 BOM+헤더 먼저 기록.
- **`LineBuffer(filepath, flush_every=100, header=None)`**: 지연 flush 버퍼. `header` 지정 시 신규 파일에 자동 헤더. 생성 시 `_line_buffers` 레지스트리에 자동 등록.
- **`flush_all_buffers()`**: 등록된 모든 LineBuffer 일괄 flush.
- **`FailedLog`**: `(post_url, reason)` 2-tuple 실패 관리. `record`/`remove`/`load_post_urls`.
- **`write_text_unique`**: slug 충돌 해소 + 파일 저장 + done CSV 기록.

### Reader

- **`load_posts(filepath)`** → `list[tuple[url, lastmod, published_time]]`
- **`load_done_file(filepath)`** → `dict[slug, post_url]`
- **`load_failed_post_urls(filepath)`** → `set[post_url]`
- **`load_image_map(filepath)`** → `dict[clean_url, relative_path]`
- **`load_stale(filepath)`** → `dict[post_url, set[clean_url]]`

### 내부 헬퍼 (download_images 등에서 사용)

- **`_split_row(line)`**: CSV 행 파싱.
- **`_is_header(line, expected)`**: BOM 제거 후 헤더 매칭.
- **`filter_file_lines(filepath, keep_fn)`**: 헤더 보존하며 in-place 필터링.
