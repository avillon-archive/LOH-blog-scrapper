# run_all.py

## CLI 옵션

| 플래그 | 설명 |
|--------|------|
| (없음) | 전체 파이프라인 실행 (html → images → {md, html-local}) |
| `--images` | 이미지만 처리 |
| `--md` | MD만 처리 |
| `--html` | HTML만 처리 |
| `--html-local` | 오프라인 열람용 HTML 생성 |
| `--retry` | 실패 목록 재처리 (원본/Wayback만) |
| `--retry-fallback` | 실패 이미지 multilang/kakao 폴백 (별도 디렉토리에 보존) |
| `--posts` | all_posts.txt를 소스로 사용 (사이트맵 갱신 건너뜀) |
| `--pages` | all_pages.txt를 소스로 사용 (사이트맵 갱신 건너뜀) |
| `--custom` | custom_posts.txt를 소스로 사용 (사이트맵 갱신 건너뜀) |
| `--force` | 기존 기록 무시, 전체 재다운로드 |
| `--sample N` | 랜덤 샘플 (행 수의 10% 상한) |
| `--seed N` | 샘플링 고정 시드 |

---

## HTML 항상 실행

`selected = user_selected | {"html"}` — `--images`나 `--md`만 지정해도 HTML 포함. 단, 사용자가 `--html`을 명시하지 않으면 HTML 단계에 `retry_mode`/`force_download`를 전달하지 않음 (보조 단계).

## CLI 옵션 제약

- `--posts`, `--pages`, `--custom`은 상호 배타적.
- `--sample N`: `all_links.txt` 유효 행 수의 **10%를 상한**으로 클램핑. `--posts`/`--pages`/`--custom`과 동시 불가.
- `--retry`: 원본/Wayback만. `--retry-fallback`: multilang/kakao 폴백 (별도 파이프라인, `images_fallback/`에 보존).

## 동적 워커·rate limit

기본값은 `config.py`(`[network]` 섹션)에서 관리. 배치 크기에 따라 동적 조정:

```
posts ≤ 100건: workers = min(len(posts), 32), rate_limit = blog_rate_limit_small (기본 20 req/s)
posts > 100건: workers = default_max_workers (기본 8), rate_limit = blog_rate_limit (기본 10 req/s)
```

## 사이트맵 갱신

**기본 모드**: `all_links.txt` 첫 줄 날짜 vs `fetch_newest_sitemap_date()`(posts+pages 양쪽 최대값). 불일치 시 전체 재빌드. pages 갱신 실패 시에도 기존 `all_pages.txt`로 links 생성 시도.

**`--posts`/`--pages`**: `_maybe_refresh_single()`로 해당 사이트맵만 개별 체크.

**`--custom`**: 사이트맵 갱신 건너뜀.

## 안전 중단 (Graceful Shutdown)

2단계 `Ctrl+C` 핸들러 (`signal.SIGINT`):

1. **1회**: `shutdown_event.set()` → 진행 중인 작업 완료 대기, 대기열 취소, 남은 파이프라인 단계 건너뜀 → `flush_all_buffers()` → `sys.exit(130)`.
2. **2회**: 즉시 `flush_all_buffers()` → `sys.exit(1)`.

정상 종료 시에도 `flush_all_buffers()`를 안전망으로 호출한다.

---

## 파이프라인 실행

### html 단계

1. KO HTML 다운로드 → `fill_published_times()` (posts + pages 모두) → `build_links_and_write()`.
2. `build_multilang_and_write()` → EN/JA 포스트+페이지 목록 생성.
3. EN/JA HTML 다운로드 → 각 언어별 `fill_published_times()`.
4. KO + EN/JA `build_html_index()` merge → `html_index` 구축.

### images 단계

`--retry-fallback`이면 `run_fallback_images()`, 아니면 `run_images()`.

### md 단계

`run_md(posts, ..., html_index=html_index)`.

### html-local 단계

`run_html_local()`. 사용자가 `--html-local`을 명시하지 않으면 `retry_mode`/`force_download`를 전달하지 않음 (html과 동일 패턴). 완료 후 `generate_listing_pages()` 자동 호출.
